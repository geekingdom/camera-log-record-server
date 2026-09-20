// 节点性能策略浏览器验收：接口全程模拟，不连接真实 Worker 或设备。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const baseUrl = process.env.BASE_URL || "http://127.0.0.1:5173";
const defaultPolicy = {
  mode: "AUTO", bufferMiB: 64, writeConcurrency: 4, batchKiB: 256,
  flushIntervalMs: 100, readMiBPerSecond: 50,
};

function nodePage(items) {
  return { items, total: items.length, page: 1, pageSize: 20 };
}

try {
  await mkdir("output/playwright", { recursive: true });
  for (const width of [1440, 390]) {
    const writes = [];
    let registeredNodes = [];
    const context = await browser.newContext({ viewport: { width, height: 900 } });
    await context.addInitScript(() => sessionStorage.setItem("camera-log-record-token", "node-performance-fixture"));
    await context.route("**/api/v1/**", route => {
      const request = route.request();
      const url = new URL(request.url());
      const path = url.pathname;
      const json = body => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
      if (path === "/api/v1/auth/me") return json({ user: { id: "admin", username: "admin", displayName: "性能验收", isAdmin: true, enabled: true, mustChangePassword: false, scopes: ["*"] } });
      if (path === "/api/v1/platform-settings") return json({ retentionDays: 7, clusterCapacity: 500, liveLogBufferMiB: 10, version: 1 });
      if (path === "/api/v1/admin/nodes" && request.method() === "POST") {
        const body = request.postDataJSON();
        writes.push({ method: "POST", body });
        const node = { ...body, version: 1, registered: true, online: false, reportedAt: null };
        registeredNodes = [node];
        return json(node);
      }
      if (path.startsWith("/api/v1/admin/nodes/") && request.method() === "PATCH") {
        const body = request.postDataJSON();
        writes.push({ method: "PATCH", body });
        const id = decodeURIComponent(path.split("/").at(-1));
        registeredNodes = registeredNodes.map(node => node.id === id ? { ...node, ...body, version: node.version + 1 } : node);
        return json(registeredNodes.find(node => node.id === id));
      }
      if (path === "/api/v1/admin/nodes") return json({ items: registeredNodes });
      if (path === "/api/v1/nodes") {
        return json(nodePage([{ id: `worker-${width}`, name: "采集节点", url: "http://worker.example.test:8001", capacity: 8, accepting: true, activeTasks: 2, online: true, reportedAt: "2026-09-20T08:00:00Z", writeLatencyMs: 12, writeLatencyLimitMs: 200, diskPercent: 30, performanceStatus: { effective: { mode: "MANUAL", bufferMiB: 128, writeConcurrency: 6, batchKiB: 512, flushIntervalMs: 80, readMiBPerSecond: 75 }, state: "MANUAL", reason: "管理员固定参数", admissionBlocked: true, changedAt: "2026-09-20T08:00:00Z", resources: {}, buffer: { reservedBytes: 33554432, limitBytes: 134217728, oldestPendingMs: 45, scope: "APPLICATION_BATCH_RESERVATIONS" }, writeIo: { stages: { batchWaitMs: { samples: 12, p99Ms: 48.5 } } } } }]));
      }
      return json(nodePage([]));
    });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(baseUrl, { waitUntil: "networkidle" });

    await page.getByRole("tab", { name: "后台配置", exact: true }).click();
    await page.getByText("节点准入", { exact: true }).click();
    await page.getByRole("button", { name: "登记节点", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "登记节点", exact: true });
    await dialog.getByRole("textbox").nth(0).fill(`performance-${width}`);
    await dialog.getByRole("textbox").nth(1).fill("http://node.example.test:18081");
    await dialog.getByText("手动", { exact: true }).click();
    await dialog.getByRole("spinbutton", { name: "日志缓冲容量", exact: true }).fill("128");
    await dialog.getByRole("spinbutton", { name: "写入并发数", exact: true }).fill("6");
    // 以可见的编辑状态留存桌面和窄屏证据；正文滚动到底部后仍须保留固定操作区。
    await dialog.locator(".el-dialog__body").evaluate(body => { body.scrollTop = body.scrollHeight; });
    const editorGeometry = await page.evaluate(() => ({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth }));
    assert.ok(editorGeometry.scrollWidth <= editorGeometry.width + 1, JSON.stringify(editorGeometry));
    await page.screenshot({ path: `output/playwright/node-performance-editor-${width}.png`, fullPage: false });
    await dialog.getByRole("button", { name: "保存配置", exact: true }).click();
    const createConfirmation = page.getByRole("dialog", { name: "确认保存配置", exact: true });
    await createConfirmation.getByRole("button", { name: "确认", exact: true }).click();
    await createConfirmation.waitFor({ state: "hidden" });
    await dialog.waitFor({ state: "hidden" });
    assert.deepEqual(writes.at(-1), { method: "POST", body: { id: `performance-${width}`, url: "http://node.example.test:18081", capacity: 10, accepting: true, inputRateLimitMiB: 50, writeLatencyLimitMs: 200, isGeneralNode: true, resourceNetworks: [], performance: { ...defaultPolicy, mode: "MANUAL", bufferMiB: 128, writeConcurrency: 6 } } });

    await page.getByRole("button", { name: "编辑节点配置", exact: true }).click();
    const editDialog = page.getByRole("dialog", { name: "编辑节点配置", exact: true });
    assert.equal(await editDialog.getByRole("radio", { name: "手动", exact: true }).isChecked(), true);
    assert.equal(await editDialog.getByRole("spinbutton", { name: "日志缓冲容量", exact: true }).inputValue(), "128");
    assert.equal(await editDialog.getByRole("spinbutton", { name: "写入并发数", exact: true }).inputValue(), "6");
    await editDialog.getByRole("spinbutton", { name: "写入并发数", exact: true }).fill("7");
    await editDialog.getByRole("button", { name: "保存配置", exact: true }).click();
    const updateConfirmation = page.getByRole("dialog", { name: "确认保存配置", exact: true });
    await updateConfirmation.getByRole("button", { name: "确认", exact: true }).click();
    await updateConfirmation.waitFor({ state: "hidden" });
    assert.deepEqual(writes.at(-1), { method: "PATCH", body: { version: 1, capacity: 10, accepting: true, inputRateLimitMiB: 50, writeLatencyLimitMs: 200, isGeneralNode: true, resourceNetworks: [], performance: { ...defaultPolicy, mode: "MANUAL", bufferMiB: 128, writeConcurrency: 7 } } });
    await page.screenshot({ path: `output/playwright/node-performance-policy-${width}.png`, fullPage: true });

    await page.getByRole("tab", { name: "服务节点", exact: true }).click();
    const status = page.getByRole("region", { name: "节点采集性能状态", exact: true });
    await status.waitFor();
    const statusText = await status.textContent();
    assert.match(statusText ?? "", /手动参数/);
    assert.match(statusText ?? "", /128 MiB 缓冲；6 路写入；512 KiB 批量；80 ms 最长批等待；75 MiB\/s 后台逻辑读取/);
    assert.match(statusText ?? "", /缓冲积压：32.0 MiB \/ 128 MiB；45 ms 最早等待；应用批次预留（不含协议库和操作系统缓存）/);
    assert.match(statusText ?? "", /管理员固定参数/);
    assert.match(statusText ?? "", /性能保护已暂停本节点新任务准入；已建立的采集会话继续运行。/);
    assert.match(statusText ?? "", /批次等待 P99：48.5 ms（12 个样本）/);
    const geometry = await page.evaluate(() => ({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth }));
    assert.ok(geometry.scrollWidth <= geometry.width + 1, JSON.stringify(geometry));
    await page.screenshot({ path: `output/playwright/node-performance-status-${width}.png`, fullPage: true });
    assert.deepEqual(errors, []);
    await context.close();
  }
  console.log(JSON.stringify({ passed: true, viewports: [1440, 390] }));
} finally {
  await browser.close();
}
