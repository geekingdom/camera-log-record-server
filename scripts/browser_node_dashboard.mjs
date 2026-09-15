// 节点看板浏览器验收：节点、遥测和健康均为页面 mock，不访问真实 Worker。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const baseUrl = process.env.BASE_URL || process.env.BROWSER_BASE_URL || "http://127.0.0.1:5173";
const screenshots = process.env.BROWSER_SCREENSHOTS || "output/playwright";
const now = Date.now();
const fresh = new Date(now - 2_000).toISOString();
const stale = new Date(now - 70_000).toISOString();
let assessedAt = new Date(now).toISOString();
const nodes = [
  {
    id: "healthy-node", name: "采集节点 A", url: "https://worker-a.example.test", heartbeat: fresh,
    accepting: true, capacity: 12, activeTasks: 3, diskPercent: 42, inputBytesPerSecond: 3_145_728, writeLatencyLimitMs: 500, writeLatencyMs: 28, writeLatencySamples: 84,
    telemetry: { sampledAt: fresh, scope: "HOST", cpuPercent: 36, memoryPercent: 58, memoryUsedBytes: 4_000_000_000, memoryTotalBytes: 8_000_000_000, networkUploadBytesPerSecond: 1_024_000, networkDownloadBytesPerSecond: 2_048_000 },
    health: { status: "HEALTHY", reasons: [] },
  },
  {
    id: "warning-node", name: "采集节点 B", url: "https://worker-b.example.test", heartbeat: fresh,
    accepting: false, capacity: 8, activeTasks: 8, diskPercent: 92, writeLatencyMs: 460, writeLatencySamples: 5,
    telemetry: { sampledAt: fresh, scope: "RUNTIME", cpuPercent: 87, memoryPercent: 91, networkUploadBytesPerSecond: 512, networkDownloadBytesPerSecond: 1_024 },
    health: { status: "WARNING", reasons: ["磁盘使用率较高", "写入延迟超过阈值"] },
  },
  {
    id: "missing-node", name: "暂无遥测节点", url: "https://worker-c.example.test", heartbeat: fresh,
    accepting: false, capacity: 4, activeTasks: 0, diskPercent: 96,
    telemetry: { sampledAt: stale, scope: "HOST", cpuPercent: 11 },
    health: { status: "CRITICAL", reasons: ["节点已隔离", "磁盘使用率过高（96.0%）"] },
  },
  {
    id: "offline-node", name: "断线节点", url: "https://worker-d.example.test", heartbeat: new Date(now - 31_000).toISOString(),
    accepting: true, capacity: 5, activeTasks: 1, diskPercent: 20,
  },
];
const pageOf = items => ({ items, total: items.length, page: 1, pageSize: 100 });

async function assertViewport(page, width) {
  const geometry = await page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth }));
  assert.ok(geometry.scrollWidth <= geometry.clientWidth + 1, `${width}px 出现横向溢出：${JSON.stringify(geometry)}`);
}

const browser = await chromium.launch({ headless: true, channel: "chrome" });
try {
  await mkdir(screenshots, { recursive: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await context.addInitScript(() => sessionStorage.setItem("camera-log-record-token", "node-dashboard"));
  await context.route("**/api/v1/**", async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const json = body => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
    if (path === "/api/v1/auth/me") return json({ user: { id: "admin", username: "admin", displayName: "验收管理员", isAdmin: true, enabled: true, mustChangePassword: false, scopes: ["*"] } });
    if (path === "/api/v1/nodes") return json(pageOf(nodes.map(node => ({ ...node, assessedAt }))));
    if (["/api/v1/resources", "/api/v1/tasks", "/api/v1/command-templates", "/api/v1/users/creators"].includes(path)) return json(pageOf([]));
    throw new Error(`未模拟接口：${request.method()} ${path}`);
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await page.getByRole("tab", { name: "服务节点", exact: true }).click();
  await page.getByText("采集节点 A", { exact: true }).waitFor();
  await page.getByText("4", { exact: true }).first().waitFor();
  await page.getByText("可接收新任务", { exact: true }).waitFor();
  await page.getByText("采集输入", { exact: true }).first().waitFor();
  await page.getByText("28 ms / 500 ms", { exact: true }).waitFor();
  await page.getByText("460 ms / 200 ms", { exact: true }).waitFor();
  await page.getByText("磁盘使用率较高；写入延迟超过阈值", { exact: true }).waitFor();
  await page.getByText("节点已隔离；磁盘使用率过高（96.0%）", { exact: true }).waitFor();
  await page.getByText("严重", { exact: true }).waitFor();
  await page.getByText("尚无新鲜遥测数据，未显示健康结论。", { exact: true }).waitFor();
  await page.getByText("断线节点", { exact: true }).waitFor();
  await page.getByText("失联", { exact: true }).last().waitFor();
  // 模拟页面驻留后 Worker 的新心跳；渲染时钟不能固定在组件创建时刻。
  await page.clock.install();
  await page.clock.fastForward(10_000);
  assessedAt = new Date(now + 10_000).toISOString();
  nodes[0].heartbeat = assessedAt;
  nodes[0].telemetry.sampledAt = nodes[0].heartbeat;
  await refresh();
  const healthyCard = page.locator(".node-card").filter({ hasText: "采集节点 A" });
  await healthyCard.getByText("在线", { exact: true }).waitFor();
  // 客户端墙上时钟偏移八小时，不应影响服务端快照的新鲜度。
  await page.clock.setSystemTime(new Date(now + 8 * 3600_000));
  await refresh();
  await healthyCard.getByText("在线", { exact: true }).waitFor();
  await healthyCard.getByText("36%", { exact: true }).waitFor();
  const originalOrder = await page.locator(".node-identity code").allTextContents();
  nodes.reverse();
  await refresh();
  assert.deepEqual(await page.locator(".node-identity code").allTextContents(), originalOrder);
  async function refresh() {
    const response = page.waitForResponse(result => new URL(result.url()).pathname === "/api/v1/nodes");
    await page.getByRole("button", { name: "刷新列表", exact: true }).click();
    await response;
    await page.clock.runFor(1100);
    await page.locator(".node-card-list .el-loading-mask").waitFor({ state: "hidden" });
  }
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 844 });
    await page.mouse.move(0, 0);
    await page.clock.runFor(300);
    await assertViewport(page, width);
    await page.screenshot({ path: `${screenshots}/node-dashboard-${width}.png`, fullPage: true, animations: "disabled" });
    const tags = await page.locator(".node-state-pill").evaluateAll(items => items.map(item => ({ width: item.getBoundingClientRect().width, text: item.textContent, opacity: getComputedStyle(item).opacity, transform: getComputedStyle(item).transform })));
    assert.ok(tags.every(tag => tag.width >= 45 && Number(tag.opacity) > .9), `状态标签不可读: ${JSON.stringify(tags)}`);
  }
  assert.deepEqual(errors, []);
  await context.close();
  console.log("节点看板浏览器验收通过，已检查健康、异常、无数据、断线与 1440px/390px 截图");
} finally {
  await browser.close();
}
