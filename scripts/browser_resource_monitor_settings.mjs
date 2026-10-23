// 资源监控规则浏览器验收：模拟API，绝不向真实设备发送配置或命令。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const baseUrl = process.env.BASE_URL || "http://127.0.0.1:5173";
const config = {
  intervalSeconds: 60, retentionDays: 90,
  items: [{ id: "memory", name: "MemAvailable", command: "cat /proc/meminfo", pattern: "MemAvailable:\\s*(\\d+) kB", unit: "KB", enabled: true }],
  processDiscoveryCommand: "ps", processRules: [{ id: "dsp", name: "Dsp_Main", pattern: ".*/hikdsp", nameGroup: null, enabled: true }],
  processStatusCommand: "cat /proc/{pid}/status", processValuePattern: "VmRSS:\\s*(\\d+) kB",
};
const writes = [];
const nodeWrites = [];
try {
  await mkdir("output/playwright", { recursive: true });
  for (const width of [1440, 390]) {
    let platform = { retentionDays: 7, clusterCapacity: 500, liveLogBufferMiB: 10, serviceLogRetentionDays: 7, logSegmentMiB: 10, version: 1, resourceMonitor: structuredClone(config) };
    let registeredNodes = [];
    const context = await browser.newContext({ viewport: { width, height: 900 } });
    await context.addInitScript(() => sessionStorage.setItem("camera-log-record-token", "monitor-settings-fixture"));
    await context.route("**/api/v1/**", route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      const json = body => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
      if (path === "/api/v1/auth/me") return json({ user: { id: "admin", username: "admin", displayName: "监控验收", isAdmin: true, enabled: true, scopes: ["*"] } });
      if (path === "/api/v1/platform-settings") {
        if (request.method() === "PATCH") {
          const body = request.postDataJSON();
          assert.equal(body.version, platform.version);
          assert.equal(body.retentionDays, 7, "指标保存不能改动日志保留配置");
          writes.push(body);
          platform = { ...platform, ...body, version: platform.version + 1 };
        }
        return json(platform);
      }
      if (path === "/api/v1/admin/nodes") {
        if (request.method() === "POST") {
          const body = request.postDataJSON();
          nodeWrites.push(body);
          const node = { ...body, version: 1, registered: true, online: false };
          registeredNodes = [node];
          return json(node);
        }
        return json({ items: registeredNodes });
      }
      if (path.startsWith("/api/v1/admin/nodes/") && request.method() === "PATCH") {
        const body = request.postDataJSON();
        nodeWrites.push(body);
        registeredNodes = registeredNodes.map(node => node.id === decodeURIComponent(path.split("/").at(-1)) ? { ...node, ...body, version: node.version + 1 } : node);
        return json(registeredNodes.find(node => node.id === decodeURIComponent(path.split("/").at(-1))));
      }
      if (["/api/v1/admin/nodes", "/api/v1/nodes", "/api/v1/tasks", "/api/v1/resources", "/api/v1/command-templates", "/api/v1/users/creators"].includes(path)) return json({ items: [], total: 0, page: 1, pageSize: 100 });
      throw new Error(`未模拟请求 ${request.method()} ${path}`);
    });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(`${baseUrl.replace(/\/$/, "")}/#resources`, { waitUntil: "networkidle" });
    await page.getByRole("tab", { name: "后台配置", exact: true }).click();
    const modules = page.locator(".settings-module-tabs");
    const platformModule = modules.getByText("平台与容量", { exact: true });
    await platformModule.waitFor();
    await page.getByRole("spinbutton", { name: "实时日志内存上限", exact: true }).fill("24");
    await page.getByRole("spinbutton", { name: "服务运行日志保留天数", exact: true }).fill("14");
    await page.getByRole("spinbutton", { name: "日志分片大小", exact: true }).fill("25");
    await page.getByRole("button", { name: "保存平台与容量配置", exact: true }).click();
    const platformConfirmation = page.getByRole("dialog", { name: "确认保存配置", exact: true });
    await platformConfirmation.getByRole("button", { name: "确认", exact: true }).click();
    await platformConfirmation.waitFor({ state: "hidden" });
    await page.getByText("平台配置已保存", { exact: true }).waitFor();
    assert.equal(writes.at(-1).liveLogBufferMiB, 24);
    assert.equal(writes.at(-1).serviceLogRetentionDays, 14);
    assert.equal(writes.at(-1).logSegmentMiB, 25);
    assert.equal(await page.getByRole("spinbutton", { name: "实时日志内存上限", exact: true }).inputValue(), "24");
    assert.equal(await page.getByRole("spinbutton", { name: "服务运行日志保留天数", exact: true }).inputValue(), "14");
    assert.equal(await page.getByRole("spinbutton", { name: "日志分片大小", exact: true }).inputValue(), "25");
    await page.screenshot({ path: `output/playwright/platform-settings-${width}.png`, fullPage: false });

    await modules.getByText("记录保留", { exact: true }).click();
    const settings = page.getByRole("region", { name: "CPU 与内存监控配置", exact: true });
    const retention = page.locator(".record-retention");
    await retention.getByRole("spinbutton", { name: "审计记录保留天数", exact: true }).waitFor();
    await retention.getByRole("spinbutton", { name: "审计记录保留天数", exact: true }).fill("30");
    await retention.getByRole("button", { name: "保存策略", exact: true }).click();
    await page.getByRole("dialog", { name: "确认保存保留策略", exact: true }).getByRole("button", { name: "确认", exact: true }).click();
    await page.getByText("增长记录保留策略已保存", { exact: true }).waitFor();
    await page.getByRole("dialog", { name: "确认保存保留策略", exact: true }).waitFor({ state: "hidden" });
    assert.deepEqual(writes.at(-1).recordRetention, { auditDays: 30, eventDays: 90, runDays: 90 });
    await retention.scrollIntoViewIfNeeded();
    await page.screenshot({ path: `output/playwright/record-retention-${width}.png`, fullPage: false });
    await modules.getByText("资源监控", { exact: true }).click();
    await settings.getByText("MemAvailable", { exact: true }).waitFor();
    assert.equal(await settings.getByRole("textbox", { name: "采集命令 1", exact: true }).count(), 0,
      "系统指标默认应折叠，避免配置页被详细规则撑长");
    await settings.getByText("MemAvailable", { exact: true }).click();
    await settings.getByRole("textbox", { name: "采集命令 1", exact: true }).waitFor();
    assert.equal(await settings.getByRole("textbox", { name: "采集命令 1", exact: true }).inputValue(), config.items[0].command);
    assert.equal(await settings.getByRole("textbox", { name: "数值正则 1", exact: true }).inputValue(), config.items[0].pattern);
    await settings.getByRole("button", { name: "新增指标", exact: true }).click();
    const newName = settings.getByRole("textbox", { name: "指标名称 2", exact: true });
    await newName.waitFor();
    await page.waitForTimeout(400);
    assert.equal(await newName.evaluate(element => document.activeElement === element), true,
      "新增指标应展开并聚焦，不能要求管理员手动滚动定位");
    const newBox = await newName.boundingBox();
    assert.ok(newBox && newBox.y >= 0 && newBox.y + newBox.height <= 900, `新增指标输入应在当前视口可见: ${JSON.stringify(newBox)}`);
    await newName.fill("Slab");
    await settings.getByRole("textbox", { name: "采集命令 2", exact: true }).fill("cat /proc/meminfo");
    await settings.getByRole("textbox", { name: "数值正则 2", exact: true }).fill("Slab:\\s*(\\d+) kB");
    await modules.getByText("记录保留", { exact: true }).click();
    await modules.getByText("资源监控", { exact: true }).click();
    assert.equal(await settings.getByRole("textbox", { name: "指标名称 2", exact: true }).inputValue(), "Slab",
      "切换配置模块不得销毁未保存的监控草稿");
    const count = writes.length;
    await settings.getByRole("button", { name: "保存监控配置", exact: true }).click();
    const confirmation = page.getByRole("dialog", { name: "确认保存监控配置", exact: true });
    await confirmation.waitFor();
    assert.equal(writes.length, count, "确认前不能提交");
    await confirmation.getByRole("button", { name: "确认", exact: true }).click();
    await page.getByText("监控配置已保存", { exact: true }).waitFor();
    assert.equal(writes.length, count + 1);
    assert.equal(writes.at(-1).resourceMonitor.items.length, 2);
    await settings.getByRole("button", { name: "删除指标 2", exact: true }).click();
    const remove = page.getByRole("dialog", { name: "确认删除规则", exact: true });
    await remove.getByRole("button", { name: "确认", exact: true }).click();
    assert.equal(await settings.getByRole("textbox", { name: "指标名称 2", exact: true }).count(), 0);
    const geometry = await page.evaluate(() => ({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth }));
    assert.ok(geometry.scrollWidth <= geometry.width + 1, JSON.stringify(geometry));
    await settings.scrollIntoViewIfNeeded();
    await page.screenshot({ path: `output/playwright/resource-monitor-settings-${width}.png`, fullPage: true });
    await modules.getByText("节点准入", { exact: true }).click();
    await page.getByRole("button", { name: "登记节点", exact: true }).click();
    const nodeDialog = page.getByRole("dialog", { name: "登记节点", exact: true });
    await nodeDialog.getByRole("textbox").nth(0).fill(`dedicated-${width}`);
    await nodeDialog.getByRole("textbox").nth(1).fill("http://node.example.test:18081");
    const rate = nodeDialog.getByRole("spinbutton", { name: "日志输入速率上限", exact: true });
    assert.equal(await rate.inputValue(), "50");
    await rate.fill("24");
    const latency = nodeDialog.getByRole("spinbutton", { name: "写入延迟上限", exact: true });
    assert.equal(await latency.inputValue(), "200");
    await latency.fill("750");
    const general = nodeDialog.getByRole("switch", { name: "通用节点", exact: true });
    assert.equal(await general.getAttribute("aria-checked"), "true");
    await nodeDialog.locator('.el-switch:has(input[aria-label="通用节点"])').click();
    await nodeDialog.getByRole("textbox", { name: "允许接入的设备资源地址", exact: true }).fill("10.41.203.35\n10.18.117.0/24");
    await nodeDialog.getByRole("button", { name: "保存配置", exact: true }).click();
    await page.getByRole("dialog", { name: "确认保存配置", exact: true }).getByRole("button", { name: "确认", exact: true }).click();
    await nodeDialog.waitFor({ state: "hidden" });
    assert.equal(nodeWrites.at(-1).isGeneralNode, false);
    assert.equal(nodeWrites.at(-1).inputRateLimitMiB, 24);
    assert.equal(nodeWrites.at(-1).writeLatencyLimitMs, 750);
    assert.deepEqual(nodeWrites.at(-1).resourceNetworks, ["10.41.203.35", "10.18.117.0/24"]);
    await page.getByRole("button", { name: "编辑节点配置", exact: true }).click();
    const editDialog = page.getByRole("dialog", { name: "编辑节点配置", exact: true });
    assert.equal(await editDialog.getByRole("spinbutton", { name: "写入延迟上限", exact: true }).inputValue(), "750");
    await editDialog.getByRole("spinbutton", { name: "写入延迟上限", exact: true }).fill("1200");
    // 推荐区扩展了正文；先让目标字段进入滚动可视区，再检查它没有被固定保存区覆盖。
    const latencyField = editDialog.getByRole("spinbutton", { name: "写入延迟上限", exact: true });
    await latencyField.scrollIntoViewIfNeeded();
    const fieldBox = await latencyField.boundingBox();
    const bodyBox = await editDialog.locator('.el-dialog__body').boundingBox();
    const saveBox = await editDialog.getByRole("button", { name: "保存配置", exact: true }).boundingBox();
    assert.ok(fieldBox && bodyBox && saveBox && fieldBox.y >= bodyBox.y && fieldBox.y + fieldBox.height <= bodyBox.y + bodyBox.height && fieldBox.y + fieldBox.height <= saveBox.y,
      "写入延迟输入不得被保存区遮挡");
    assert.ok(saveBox.y + saveBox.height <= 900, "保存区应在视口内可见");
    await page.screenshot({ path: `output/playwright/node-write-latency-settings-${width}.png`, animations: "disabled" });
    await editDialog.getByRole("button", { name: "保存配置", exact: true }).click();
    await page.getByRole("dialog", { name: "确认保存配置", exact: true }).getByRole("button", { name: "确认", exact: true }).click();
    assert.equal(nodeWrites.at(-1).writeLatencyLimitMs, 1200);
    await page.getByText("1.2 s", { exact: true }).waitFor();
    assert.deepEqual(errors, []);
    await context.close();
  }
  console.log(JSON.stringify({ passed: true, writes: writes.length, nodeWrites: nodeWrites.length, viewports: [1440, 390] }));
} finally { await browser.close(); }
