// 任务列表操作验收：使用拦截的只读任务快照，验证不同状态不会展示重复生命周期按钮。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
await context.addInitScript(() => sessionStorage.setItem("camera-log-record-token", "synthetic-ui-token"));

const tasks = [
  { id: "serial", name: "采集中串口", protocol: "TELNET_SERIAL", ip: "192.0.2.1", port: 23, status: "COLLECTING", desiredState: "RUNNING" },
  { id: "device", name: "采集中设备", protocol: "TELNET_DEVICE", ip: "192.0.2.6", port: 23, status: "COLLECTING", desiredState: "RUNNING" },
  { id: "ssh", name: "采集中 SSH", protocol: "SSH", ip: "192.0.2.2", port: 22, status: "COLLECTING", desiredState: "RUNNING" },
  { id: "paused", name: "已暂停 SSH", protocol: "SSH", ip: "192.0.2.3", port: 22, status: "PAUSED", desiredState: "PAUSED" },
  { id: "stopped", name: "已停止任务", protocol: "TELNET_DEVICE", ip: "192.0.2.4", port: 23, status: "STOPPED", desiredState: "STOPPED" },
  { id: "reconnecting", name: "重连中过渡", protocol: "SSH", ip: "192.0.2.7", port: 22, status: "RECONNECTING", desiredState: "RUNNING" },
  { id: "error", name: "未归属错误", protocol: "SSH", ip: "192.0.2.8", port: 22, status: "ERROR", desiredState: "STOPPED", nodeId: null },
  { id: "blocked", name: "隔离等待", protocol: "SSH", ip: "192.0.2.9", port: 22, status: "BLOCKED", desiredState: "RUNNING" },
  { id: "stopping", name: "停止中过渡", protocol: "SSH", ip: "192.0.2.5", port: 22, status: "STOPPING", desiredState: "STOPPED" },
].map(task => ({ ...task, initialCommands: [], scheduledCommands: [] }));

await context.route("**/api/v1/**", async route => {
  const request = route.request();
  assert.equal(request.method(), "GET", "操作可见性验收不得发出修改请求");
  const url = new URL(request.url());
  const items = url.pathname === "/api/v1/tasks" ? tasks : [];
  await route.fulfill({ json: { items, total: items.length, page: 1, pageSize: 20 } });
});

const page = await context.newPage();
page.setDefaultTimeout(10000);
const errors = [];
page.on("pageerror", error => errors.push(error.message));
page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });

async function actionNames(name) {
  const row = page.locator(".el-table__body tr").filter({ hasText: name });
  return row.locator("button[aria-label]").evaluateAll(buttons =>
    buttons.map(button => button.getAttribute("aria-label")).filter(label =>
      ["启动任务", "停止任务", "暂停任务", "继续任务"].includes(label),
    ),
  );
}

try {
  await mkdir("output/playwright", { recursive: true });
  await page.goto("http://127.0.0.1:5173", { waitUntil: "networkidle" });
  await page.getByText("采集中串口", { exact: true }).waitFor();
  assert.deepEqual(await actionNames("采集中串口"), ["停止任务"]);
  assert.deepEqual(await actionNames("采集中设备"), ["停止任务"]);
  assert.deepEqual(await actionNames("采集中 SSH"), ["停止任务", "暂停任务"]);
  assert.deepEqual(await actionNames("已暂停 SSH"), ["停止任务", "继续任务"]);
  assert.deepEqual(await actionNames("已停止任务"), ["启动任务"]);
  assert.deepEqual(await actionNames("重连中过渡"), ["停止任务"]);
  assert.deepEqual(await actionNames("未归属错误"), ["启动任务"]);
  assert.deepEqual(await actionNames("隔离等待"), []);
  assert.deepEqual(await actionNames("停止中过渡"), []);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.waitForTimeout(200);
    const pageOverflows = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
    assert.equal(pageOverflows, false, "页面不得出现无意横向溢出");
    if (width === 390) {
      const table = page.locator(".el-table__body-wrapper .el-scrollbar__wrap").first();
      await table.evaluate(element => { element.scrollLeft = element.scrollWidth; });
      const actionBox = await page.locator(".el-table__body tr")
        .filter({ hasText: "已停止任务" }).getByLabel("启动任务").boundingBox();
      assert.ok(actionBox && actionBox.x >= 0 && actionBox.x + actionBox.width <= width, "移动端操作列不可达");
    }
    await page.screenshot({ path: `output/playwright/task-actions-${width}.png`, fullPage: true });
  }
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, checked: tasks.length, viewports: [1440, 390], consoleErrors: 0, taskMutations: 0 }));
} finally {
  await browser.close();
}
