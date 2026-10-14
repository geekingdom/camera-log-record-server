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
    const recommendedId = `recommended-${width}`;
    const recommendation = {
      available: true,
      reason: "依据新鲜容器配额与可用内存生成推荐值。",
      policy: { mode: "AUTO", bufferMiB: 128, writeConcurrency: 6, batchKiB: 256, flushIntervalMs: 100, readMiBPerSecond: 25 },
      resources: { effectiveCpu: 6, memoryLimitBytes: 0, memoryAvailableBytes: 1073741824, source: "CGROUP_V2" },
      reportedAt: "2026-09-20T08:00:00Z",
    };
    const discoveredNode = { id: recommendedId, url: "http://recommendation.example.test:18081", capacity: 8, accepting: true, version: 0, registered: false, online: true, reportedAt: recommendation.reportedAt, performance: defaultPolicy, performanceRecommendation: recommendation };
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
      if (path === "/api/v1/admin/nodes") return json({ items: [...registeredNodes, discoveredNode] });
      if (path === "/api/v1/nodes") {
        const archives = width === 1440
          ? { pendingJobs: 5, waitingJobs: 3, runningJobs: 2, oldestSeconds: 45, oldestWaitingSeconds: 13, oldestRunningSeconds: 45 }
          : { pendingJobs: 4, oldestSeconds: 18 };
        return json(nodePage([{ id: `worker-${width}`, name: "采集节点", url: "http://worker.example.test:8001", capacity: 8, accepting: true, activeTasks: 2, online: true, reportedAt: "2026-09-20T08:00:00Z", writeLatencyMs: 12, writeLatencyLimitMs: 200, diskPercent: 30, performanceStatus: { effective: { mode: "MANUAL", bufferMiB: 128, writeConcurrency: 6, batchKiB: 512, flushIntervalMs: 80, readMiBPerSecond: 75 }, state: "MANUAL", reason: "管理员固定参数", admissionBlocked: true, changedAt: "2026-09-20T08:00:00Z", resources: {}, buffer: { reservedBytes: 33554432, limitBytes: 134217728, oldestPendingMs: 45, scope: "APPLICATION_BATCH_RESERVATIONS" }, writeIo: { stages: { batchWaitMs: { samples: 12, p99Ms: 48.5 } } }, writeIoLatency: { p99Ms: 512.4, pendingMs: 2000, samples: 24, windowSeconds: 60 }, archives } }]));
      }
      return json(nodePage([]));
    });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(`${baseUrl.replace(/\/$/, "")}/#resources`, { waitUntil: "networkidle" });

    await page.getByRole("tab", { name: "后台配置", exact: true }).click();
    await page.getByText("节点准入", { exact: true }).click();
    await page.getByRole("button", { name: "登记节点", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "登记节点", exact: true });
    const nodeId = dialog.getByRole("textbox").nth(0);
    const recommendationRegion = dialog.getByRole("region", { name: "节点性能推荐", exact: true });
    await recommendationRegion.getByText("尚未收到匹配节点 ID 的心跳，无法按服务器资源推荐性能值。", { exact: true }).waitFor();
    assert.equal(await recommendationRegion.getByRole("button", { name: "应用推荐值", exact: true }).isDisabled(), true);
    await nodeId.fill(recommendedId);
    await dialog.getByRole("textbox").nth(1).fill("http://node.example.test:18081");
    await recommendationRegion.getByText("依据新鲜容器配额与可用内存生成推荐值。", { exact: true }).waitFor();
    await recommendationRegion.getByText("有效 CPU：6；可用内存：1.0 GiB / 限制 未设置容器上限；来源：容器 cgroup v2", { exact: true }).waitFor();
    await recommendationRegion.getByText(/依据心跳：2026\/9\/20/, { exact: false }).waitFor();
    await recommendationRegion.getByRole("button", { name: "应用推荐值", exact: true }).click();
    assert.equal(await dialog.getByRole("radio", { name: "自动", exact: true }).isChecked(), true);
    assert.equal(await dialog.getByRole("spinbutton", { name: "日志缓冲容量", exact: true }).inputValue(), "128");
    assert.equal(await dialog.getByRole("spinbutton", { name: "写入并发数", exact: true }).inputValue(), "6");
    await dialog.getByRole("spinbutton", { name: "写入并发数", exact: true }).fill("7");
    await recommendationRegion.getByRole("button", { name: "刷新节点推荐", exact: true }).click();
    await page.locator('button[aria-label="刷新节点推荐"].is-loading').waitFor({ state: "hidden" });
    assert.equal(await dialog.getByRole("spinbutton", { name: "写入并发数", exact: true }).inputValue(), "7", "刷新列表不得覆盖已应用后手调的策略草稿");
    await page.screenshot({ path: `output/playwright/node-performance-recommendation-${width}.png`, fullPage: false });
    // 以可见的编辑状态留存桌面和窄屏证据；正文滚动到底部后仍须保留固定操作区。
    await dialog.locator(".el-dialog__body").evaluate(body => { body.scrollTop = body.scrollHeight; });
    const saveBox = await dialog.getByRole("button", { name: "保存配置", exact: true }).boundingBox();
    assert.ok(saveBox && saveBox.y >= 0 && saveBox.y + saveBox.height <= 900, "性能策略滚动到底后保存操作必须仍在视口内");
    const editorGeometry = await page.evaluate(() => ({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth }));
    assert.ok(editorGeometry.scrollWidth <= editorGeometry.width + 1, JSON.stringify(editorGeometry));
    await page.screenshot({ path: `output/playwright/node-performance-editor-${width}.png`, fullPage: false });
    await dialog.getByRole("button", { name: "保存配置", exact: true }).click();
    const createConfirmation = page.getByRole("dialog", { name: "确认保存配置", exact: true });
    await createConfirmation.getByRole("button", { name: "确认", exact: true }).click();
    await createConfirmation.waitFor({ state: "hidden" });
    await dialog.waitFor({ state: "hidden" });
    assert.deepEqual(writes.at(-1), { method: "POST", body: { id: recommendedId, url: "http://node.example.test:18081", capacity: 10, accepting: true, inputRateLimitMiB: 50, writeLatencyLimitMs: 200, isGeneralNode: true, resourceNetworks: [], performance: { ...recommendation.policy, writeConcurrency: 7 } } });

    await page.getByRole("button", { name: "编辑节点配置", exact: true }).click();
    const editDialog = page.getByRole("dialog", { name: "编辑节点配置", exact: true });
    assert.equal(await editDialog.getByRole("radio", { name: "自动", exact: true }).isChecked(), true);
    assert.equal(await editDialog.getByRole("spinbutton", { name: "日志缓冲容量", exact: true }).inputValue(), "128");
    assert.equal(await editDialog.getByRole("spinbutton", { name: "写入并发数", exact: true }).inputValue(), "7");
    await editDialog.getByRole("spinbutton", { name: "写入并发数", exact: true }).fill("8");
    await editDialog.getByRole("button", { name: "保存配置", exact: true }).click();
    const updateConfirmation = page.getByRole("dialog", { name: "确认保存配置", exact: true });
    await updateConfirmation.getByRole("button", { name: "确认", exact: true }).click();
    await updateConfirmation.waitFor({ state: "hidden" });
    assert.deepEqual(writes.at(-1), { method: "PATCH", body: { version: 1, capacity: 10, accepting: true, inputRateLimitMiB: 50, writeLatencyLimitMs: 200, isGeneralNode: true, resourceNetworks: [], performance: { ...recommendation.policy, writeConcurrency: 8 } } });
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
    assert.match(statusText ?? "", /实际写入 P99：512.4 ms；未完成写入等待：2000 ms（60 秒窗口，24 个样本）/);
    assert.match(statusText ?? "", width === 1440
      ? /归档：排队 3 个，最长 13 秒；执行中 2 个，最长 45 秒/
      : /归档：未完成 4 个，最长 18 秒（旧节点未区分排队与执行）/);
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
