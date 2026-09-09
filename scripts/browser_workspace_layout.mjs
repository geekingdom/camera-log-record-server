// 工作台布局与资源快捷创建验收：仅使用模拟API，不创建真实任务或连接设备。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const resources = [
  { id: "camera", name: "模拟网络设备", kind: "HIKVISION_NETWORK", ip: "192.0.2.10", model: "DS-2CD", subSerialNumber: "SN-fixture", version: 1 },
  { id: "serial", name: "模拟串口服务器", kind: "SERIAL_SERVER", ip: "192.0.2.11", version: 1 },
];
const task = { id: "task", name: "模拟采集", resourceId: "camera", protocol: "SSH", ip: "192.0.2.10", port: 22,
  status: "STOPPED", desiredState: "STOPPED", initialCommands: [], scheduledCommands: [], version: 1 };
const pageOf = items => ({ items, total: items.length, page: 1, pageSize: 100 });
await mkdir("output/playwright", { recursive: true });
try {
  for (const width of [1440, 390]) {
    let allowCreate = true;
    let resourceScope = null;
    const created = [];
    const context = await browser.newContext({ viewport: { width, height: 900 } });
    await context.route("**/api/v1/**", async route => {
      const path = new URL(route.request().url()).pathname;
      let body = pageOf([]);
      if (path === "/api/v1/auth/me") body = { user: { id: "admin", username: "fixture", displayName: "模拟管理员", scopes: allowCreate ? ["*"] : ["tasks:read", "logs:read"], resourceIds: resourceScope, isAdmin: allowCreate, mustChangePassword: false } };
      else if (path === "/api/v1/tasks" && route.request().method() === "POST") { created.push(route.request().postDataJSON()); body = { ...task, ...created.at(-1) }; }
      else if (path === "/api/v1/resources") body = pageOf(resources);
      else if (path.startsWith("/api/v1/resources/")) body = resources.find(resource => path.endsWith(resource.id));
      else if (path === "/api/v1/tasks") body = pageOf([task]);
      else if (path === "/api/v1/tasks/task") body = task;
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
    });
    await context.routeWebSocket("**/api/v1/tasks/*/logs*", socket => socket.close());
    const ui = await context.newPage();
    const errors = [];
    ui.on("pageerror", error => errors.push(error.message));
    await ui.goto(process.env.BASE_URL || "http://127.0.0.1:5173");
    await ui.getByRole("tab", { name: "日志工作台", exact: true }).click();
    await ui.getByRole("combobox", { name: "选择日志任务" }).click();
    await ui.getByRole("option", { name: /模拟采集/ }).click();
    const samples = [];
    for (const tab of ["实时打印", "小时归档与检索", "命令记录", "实时打印"]) {
      await ui.getByRole("tab", { name: tab, exact: true }).click();
      await ui.waitForTimeout(300);
      samples.push(await ui.locator(".workspace").evaluate(element => ({
        x: element.getBoundingClientRect().x, width: element.getBoundingClientRect().width,
        scrollX: window.scrollX, scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
      })));
      await ui.screenshot({ path: `output/playwright/workspace-${width}-${samples.length}.png`, fullPage: true });
    }
    console.log(JSON.stringify({ width, samples }));
    for (const sample of samples) {
      assert.ok(Math.abs(sample.x - samples[0].x) < 1, "切换页签不应移动工作区");
      assert.ok(Math.abs(sample.width - samples[0].width) < 1, "切换页签不应改变工作区宽度");
      assert.ok(sample.scrollWidth <= sample.clientWidth + 1, "页面不应横向溢出");
    }
    // Linux常驻滚动条出现/消失不能挤动布局；macOS叠加滚动条也走同一断言。
    await ui.getByRole("tab", { name: "命令记录", exact: true }).click();
    const beforeScroll = await ui.locator(".workspace").boundingBox();
    await ui.evaluate(() => { document.body.style.minHeight = "200vh"; });
    const withScroll = await ui.locator(".workspace").boundingBox();
    assert.equal(withScroll.x, beforeScroll.x);
    assert.equal(withScroll.width, beforeScroll.width, "纵向滚动条出现不应挤动主体");
    await ui.evaluate(() => { document.body.style.minHeight = ""; });
    if (width > 700) {
      await ui.getByRole("button", { name: "折叠导航栏", exact: true }).click();
      const expanded = await ui.locator(".workspace").boundingBox();
      for (const tab of ["小时归档与检索", "命令记录", "实时打印"]) {
        await ui.getByRole("tab", { name: tab, exact: true }).click();
        const bounds = await ui.locator(".workspace").boundingBox();
        assert.equal(bounds.x, expanded.x);
        assert.equal(bounds.width, expanded.width);
      }
      assert.ok(expanded.width > samples[0].width);
      await ui.getByRole("button", { name: "展开导航栏", exact: true }).click();
    }
    await ui.getByRole("tab", { name: "设备资源", exact: true }).click();
    for (const resource of resources) {
      const row = ui.locator(".resource-table .el-table__row").filter({ hasText: resource.name });
      await row.getByRole("button", { name: "新建采集任务", exact: true }).click();
      const drawer = ui.getByRole("dialog");
      await drawer.waitFor();
      await ui.waitForTimeout(400);
      const bounds = await drawer.boundingBox();
      assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= width + 1, "抽屉必须完整位于视口内");
      assert.ok(await ui.getByRole("tab", { name: "设备资源", exact: true }).getAttribute("aria-selected") === "true");
      assert.ok(await drawer.locator("input").evaluateAll((inputs, ip) => inputs.some(input => input.value === ip && input.disabled), resource.ip));
      await ui.screenshot({ path: `output/playwright/resource-task-${width}-${resource.id}.png`, fullPage: true });
      await drawer.getByLabel("任务名称", { exact: true }).fill("模拟快捷创建");
      if (resource.kind === "HIKVISION_NETWORK") {
        await drawer.getByLabel("用户名", { exact: true }).fill("fixture");
        await drawer.getByLabel("密码", { exact: true }).fill("fixture-password");
      } else await drawer.getByRole("spinbutton", { name: "端口" }).fill("10002");
      await drawer.getByRole("button", { name: "保存任务", exact: true }).click();
      await drawer.waitFor({ state: "hidden" });
      assert.equal(created.at(-1).resourceId, resource.id);
      assert.equal(created.at(-1).ip, resource.ip);
      assert.equal(created.at(-1).protocol, resource.kind === "SERIAL_SERVER" ? "TELNET_SERIAL" : "SSH");
    }
    assert.equal(created.length, 2);
    allowCreate = false;
    await ui.reload();
    await ui.locator(".resource-table .el-table__row").first().waitFor();
    assert.equal(await ui.getByRole("button", { name: "新建采集任务", exact: true }).count(), 0);
    allowCreate = true;
    resourceScope = ["camera"];
    await ui.reload();
    await ui.locator(".resource-table .el-table__row").first().waitFor();
    assert.equal(await ui.getByRole("button", { name: "新建采集任务", exact: true }).count(), 1);
    assert.equal(await ui.locator(".resource-table .el-table__row").filter({ hasText: resources[1].name })
      .getByRole("button", { name: "新建采集任务", exact: true }).count(), 0);
    assert.deepEqual(errors, []);
    await context.close();
  }
} finally {
  await browser.close();
}
