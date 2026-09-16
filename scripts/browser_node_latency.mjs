// 节点写入延迟验收：浏览器层模拟 API，覆盖告警、等待写入与窄屏表格滚动。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const output = "output/playwright";
const timestamp = "2026-09-08T08:00:00.000Z";
const nodes = [
  {
    id: "latency-warning", url: "https://node-warning.example.test:8443", heartbeat: timestamp,
    capacity: 8, activeTasks: 2, diskPercent: 44.2, accepting: true, configurationMismatch: false,
    inputBytesPerSecond: 8192, writeLatencyMs: 248, writeLatencySamples: 36,
    writeLatencyPendingMs: 0, writeLatencyWindowSeconds: 60,
  },
  {
    id: "latency-pending", url: "https://node-pending.example.test:8443", heartbeat: timestamp,
    capacity: 4, activeTasks: 1, diskPercent: 22.1, accepting: true, configurationMismatch: false,
    inputBytesPerSecond: 0, writeLatencyMs: 0, writeLatencySamples: 0,
    writeLatencyPendingMs: 315, writeLatencyWindowSeconds: 60,
  },
  {
    id: "latency-empty", url: "https://node-empty.example.test:8443", heartbeat: timestamp,
    capacity: 4, activeTasks: 0, diskPercent: 22.1, accepting: true, configurationMismatch: false,
    inputBytesPerSecond: 0, writeLatencyMs: 0, writeLatencySamples: 0,
    writeLatencyPendingMs: 0, writeLatencyWindowSeconds: 60,
  },
  {
    id: "latency-long-pending", url: "https://node-long-pending.example.test:8443", heartbeat: timestamp,
    capacity: 4, activeTasks: 1, diskPercent: 22.1, accepting: true, configurationMismatch: false,
    inputBytesPerSecond: 0, writeLatencyMs: 0, writeLatencySamples: 0,
    writeLatencyPendingMs: 3660000, writeLatencyWindowSeconds: 60,
  },
  {
    id: "latency-legacy", url: "https://node-legacy.example.test:8443", heartbeat: timestamp,
    capacity: 4, activeTasks: 0, diskPercent: 22.1, accepting: true, configurationMismatch: false,
    inputBytesPerSecond: 0,
  },
];

const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
await context.addInitScript(() => {
  sessionStorage.setItem("camera-log-record-token", "synthetic-node-latency-token");
  localStorage.setItem("camera-log-sidebar-collapsed", "false");
});
await context.routeWebSocket("**/api/v1/tasks/*/logs", (socket) => socket.close());

async function json(route, body) {
  await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
}

await context.route("**/api/v1/**", async (route) => {
  const request = route.request();
  const path = new URL(request.url()).pathname;
  if (request.method() !== "GET") throw new Error(`未模拟的写请求：${request.method()} ${path}`);
  if (path === "/api/v1/resources" || path === "/api/v1/tasks" || path === "/api/v1/command-templates" || path === "/api/v1/service-tokens"
    || path === "/api/v1/audit-events" || path === "/api/v1/runtime-events")
    return json(route, { items: [], total: 0, page: 1, pageSize: 100 });
  if (path === "/api/v1/nodes") return json(route, { items: nodes, total: nodes.length, page: 1, pageSize: 100 });
  if (path === "/api/v1/platform-settings") return json(route, { retentionDays: 7, version: 1, updatedAt: timestamp });
  if (path === "/api/v1/display-settings") return json(route, { liveLogBufferMiB: 10 });
  if (path === "/api/v1/admin/nodes") return json(route, { items: [] });
  throw new Error(`未模拟的请求：${request.method()} ${path}`);
});

const page = await context.newPage();
page.setDefaultTimeout(10000);
const issues = [];

async function assertPageWidth(label) {
  const result = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    viewport: window.innerWidth,
    offenders: [...document.querySelectorAll("*")].map((element) => {
      const box = element.getBoundingClientRect();
      return { tag: element.tagName, className: String(element.className), left: box.left, right: box.right, width: box.width };
    }).filter((item) => item.right > window.innerWidth + 1).sort((left, right) => right.right - left.right).slice(0, 8),
    navigation: [".shell", ".sidebar", ".side-nav"].map((selector) => {
      const element = document.querySelector(selector);
      const box = element?.getBoundingClientRect();
      const style = element && getComputedStyle(element);
      return { selector, width: box?.width, scrollWidth: element?.scrollWidth, overflowX: style?.overflowX };
    }),
  }));
  if (result.scrollWidth > result.viewport + 1)
    issues.push(`${label}: 页面水平溢出 ${result.scrollWidth}px > ${result.viewport}px ${JSON.stringify(result.offenders)} ${JSON.stringify(result.navigation)}`);
}

async function assertTableCanScroll() {
  const result = await page.locator(".data-table .el-scrollbar__wrap").last().evaluate((element) => {
    element.scrollLeft = 0;
    const before = element.scrollLeft;
    element.scrollLeft = element.scrollWidth;
    return { before, after: element.scrollLeft, scrollWidth: element.scrollWidth, clientWidth: element.clientWidth };
  });
  if (result.scrollWidth > result.clientWidth + 1 && result.after <= result.before)
    issues.push("移动端节点表存在横向内容但内部滚动容器不能移动");
}

async function assertMobileLatencyTags() {
  const values = ["248 ms", "等待写入 315 ms", "等待写入 61.0 min"];
  for (const value of values) {
    const tag = page.locator(".data-table .el-tag").filter({ hasText: value });
    assert.equal(await tag.count(), 1, `移动端应保留 ${value} 标签`);
    assert.equal(await tag.isVisible(), true, `移动端 ${value} 标签应可见`);
    const fits = await tag.evaluate((element) => {
      const tagBox = element.getBoundingClientRect();
      const cellBox = element.parentElement?.getBoundingClientRect();
      return Boolean(cellBox && tagBox.left >= cellBox.left && tagBox.right <= cellBox.right + 1);
    });
    assert.equal(fits, true, `移动端 ${value} 标签应位于写入延迟列内`);
  }
  assert.equal(await page.locator(".el-popper.is-dark:visible").count(), 0, "缩窗后不应保留延迟提示浮层");
}

try {
  await mkdir(output, { recursive: true });
  await page.goto("http://127.0.0.1:5173", { waitUntil: "networkidle" });
  await page.getByRole("tab", { name: "服务节点", exact: true }).click();
  await page.locator("h1").filter({ hasText: "服务节点" }).waitFor();
  await page.getByText("无样本", { exact: true }).first().waitFor();

  const warningRow = page.getByRole("row").filter({ hasText: "latency-warning" });
  const pendingRow = page.getByRole("row").filter({ hasText: "latency-pending" });
  const emptyRow = page.getByRole("row").filter({ hasText: "latency-empty" });
  const longPendingRow = page.getByRole("row").filter({ hasText: "latency-long-pending" });
  const legacyRow = page.getByRole("row").filter({ hasText: "latency-legacy" });
  assert.equal(await warningRow.locator(".el-tag--warning").count(), 1, "批次 P99 超阈值应有警示状态");
  assert.match(await pendingRow.innerText(), /等待写入\s*315 ms/, "首批等待写入应展示等待年龄");
  assert.equal(await pendingRow.locator(".el-tag--warning").count(), 1, "等待写入超阈值应有警示状态");
  assert.match(await emptyRow.innerText(), /无样本/, "无样本且无等待写入应明确标记");
  assert.doesNotMatch(await emptyRow.innerText(), /0 ms/, "无样本不可误显示为 0 ms");
  assert.match(await longPendingRow.innerText(), /等待写入 61.0 min/, "超长等待值应压缩单位并保持可读");
  const longPendingFits = await longPendingRow.locator(".el-tag").filter({ hasText: "等待写入 61.0 min" }).evaluate((tag) => {
    const tagBox = tag.getBoundingClientRect();
    const cellBox = tag.parentElement?.getBoundingClientRect();
    return Boolean(cellBox && tagBox.left >= cellBox.left && tagBox.right <= cellBox.right + 1);
  });
  assert.equal(longPendingFits, true, "超长等待值标签应位于写入延迟列内");
  assert.match(await legacyRow.innerText(), /无样本/, "旧节点缺失指标字段应降级为无样本");
  assert.doesNotMatch(await legacyRow.innerText(), /NaN|undefined/, "旧节点缺失指标字段不可显示异常值");

  const header = page.getByText("写入延迟", { exact: true }).first();
  await header.hover();
  const tooltip = page.getByText(/最慢一路近 60 秒/, { exact: false });
  await tooltip.waitFor();
  assert.match(await tooltip.innerText(), /P99/);
  assert.match(await tooltip.innerText(), /待写时长/);
  assert.match(await tooltip.innerText(), /批次等待/);
  assert.match(await tooltip.innerText(), /fsync/);
  await assertPageWidth("桌面服务节点");
  await page.screenshot({ path: `${output}/node-latency-1440x900.png`, fullPage: true, animations: "disabled" });

  await page.setViewportSize({ width: 390, height: 844 });
  await tooltip.waitFor({ state: "hidden" });
  await assertTableCanScroll();
  await assertMobileLatencyTags();
  await assertPageWidth("移动服务节点");
  await page.screenshot({ path: `${output}/node-latency-390x844.png`, fullPage: true, animations: "disabled" });
  assert.deepEqual(issues, []);
  console.log(JSON.stringify({ passed: true, screenshots: [
    `${output}/node-latency-1440x900.png`, `${output}/node-latency-390x844.png`,
  ] }));
} finally {
  await browser.close();
}
