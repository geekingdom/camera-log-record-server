// 工作台布局与资源快捷创建验收：仅使用模拟API，不创建真实任务或连接设备。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const resources = [
  { id: "camera", name: "模拟网络设备", kind: "HIKVISION_NETWORK", ip: "192.0.2.10", model: "DS-2CD", subSerialNumber: "SN-fixture", version: 1, createdBy: "admin", createdByName: "模拟管理员" },
  { id: "serial", name: "模拟串口服务器", kind: "SERIAL_SERVER", ip: "192.0.2.11", version: 1, createdBy: "admin", createdByName: "模拟管理员" },
];
const task = { id: "task", name: "模拟采集", resourceId: "camera", protocol: "SSH", ip: "192.0.2.10", port: 22,
  status: "STOPPED", desiredState: "STOPPED", initialCommands: [], scheduledCommands: [], version: 1, createdBy: "admin", createdByName: "模拟管理员", createdAt: "2026-09-09T10:00:00Z" };
const secondTask = { ...task, id: "task-second", name: "备用采集", ip: "192.0.2.12", port: 23, protocol: "TELNET_DEVICE", status: "COLLECTING" };
const pageOf = (items, page = 1, pageSize = 100) => ({ items, total: items.length, page, pageSize });
await mkdir("output/playwright", { recursive: true });
try {
  for (const width of [1440, 390, 320]) {
    let allowCreate = true;
    const created = [];
    let secondTaskReads = 0;
    const context = await browser.newContext({ viewport: { width, height: 900 } });
    const taskQueries = [];
    await context.route("**/api/v1/**", async route => {
      const url = new URL(route.request().url());
      const path = url.pathname;
      let body = pageOf([]);
      if (path === "/api/v1/auth/me") body = { user: { id: allowCreate ? "admin" : "viewer", username: "fixture", displayName: allowCreate ? "模拟管理员" : "只读操作员", scopes: allowCreate ? ["*"] : ["tasks:read", "tasks:write", "tasks:control", "resources:write", "logs:read", "commands:send"], isAdmin: allowCreate, mustChangePassword: false } };
      else if (path === "/api/v1/tasks" && route.request().method() === "POST") { created.push(route.request().postDataJSON()); body = { ...task, ...created.at(-1) }; }
      else if (path === "/api/v1/resources") body = pageOf(resources);
      else if (path.endsWith("/coredump-monitor")) body = { active: false, ownerTask: null, mountStatus: null };
      else if (path.startsWith("/api/v1/resources/")) body = resources.find(resource => path.endsWith(resource.id));
      else if (path === "/api/v1/tasks") {
        taskQueries.push(Object.fromEntries(url.searchParams));
        const search = url.searchParams.get("search") || "";
        const status = url.searchParams.get("status") || "";
        const resourceId = url.searchParams.get("resourceId") || "";
        const page = Number(url.searchParams.get("page") || 1);
        const pageSize = Number(url.searchParams.get("pageSize") || 100);
        const filtered = [task, secondTask].filter(candidate =>
          (!search || `${candidate.name} ${candidate.ip} ${candidate.port}`.includes(search)) &&
          (!status || candidate.status === status) && (!resourceId || candidate.resourceId === resourceId),
        );
        body = pageOf(filtered.slice((page - 1) * pageSize, page * pageSize), page, pageSize);
      }
      else if (path === "/api/v1/tasks/task") {
        // 旧任务详情故意迟到，验证切回新任务后不能覆盖状态栏。
        await new Promise(resolve => setTimeout(resolve, 900));
        body = task;
      }
      else if (path === "/api/v1/tasks/task-second") {
        secondTaskReads += 1;
        body = { ...secondTask, status: secondTaskReads === 1 ? "STOPPED" : "COLLECTING" };
      }
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
    });
    await context.routeWebSocket("**/api/v1/tasks/*/logs*", socket => socket.close());
    const ui = await context.newPage();
    const errors = [];
    ui.on("pageerror", error => errors.push(error.message));
    await ui.goto(process.env.BASE_URL || "http://127.0.0.1:5173");
    await ui.getByRole("tab", { name: "日志工作台", exact: true }).click();
    assert.equal(await ui.getByRole("combobox", { name: "选择日志任务" }).count(), 0, "工作台不应保留全任务下拉框");
    await ui.getByRole("button", { name: "选择任务", exact: true }).click();
    const picker = ui.getByRole("dialog", { name: "选择日志任务" });
    await picker.waitFor();
    await picker.getByRole("columnheader", { name: "创建人", exact: true }).waitFor();
    await picker.getByRole("columnheader", { name: "创建时间（北京时间）", exact: true }).waitFor();
    const resourceFilter = picker.getByRole("combobox", { name: "按设备资源筛选日志任务" });
    await resourceFilter.click();
    await ui.getByRole("option", { name: /模拟串口服务器/ }).click();
    await picker.getByText("没有符合条件的任务").waitFor();
    assert.ok(taskQueries.some(query => query.resourceId === "serial"), "资源条件必须传入服务端");
    await resourceFilter.click();
    await ui.getByRole("option", { name: /模拟网络设备/ }).click();
    await picker.getByText("模拟采集", { exact: true }).waitFor();
    assert.ok(await picker.locator(".el-table__row").first().textContent().then(text => text.includes("模拟管理员") && text.includes("18:00:00")));
    await ui.getByRole("option", { name: /模拟网络设备/ }).waitFor({ state: "hidden" });
    await ui.screenshot({ path: `output/playwright/task-picker-${width}.png`, fullPage: true, animations: "disabled" });
    await picker.getByRole("textbox", { name: "搜索日志任务" }).fill("备用");
    await picker.getByRole("button", { name: "搜索日志任务" }).click();
    await picker.getByText("备用采集", { exact: true }).click();
    await picker.getByRole("button", { name: "确认切换", exact: true }).click();
    await picker.waitFor({ state: "hidden" });
    await ui.getByText("备用采集", { exact: true }).waitFor();
    await ui.getByText("TELNET_DEVICE", { exact: true }).waitFor();
    await ui.getByText("192.0.2.12:23", { exact: true }).waitFor();
    await ui.getByText("已停止", { exact: true }).waitFor();
    assert.ok(taskQueries.some(query => query.search === "备用" && query.page === "1" && query.pageSize === "10"), "任务选择必须使用服务端搜索和分页");
    await ui.waitForTimeout(5200);
    await ui.getByText("采集中", { exact: true }).waitFor();
    assert.ok(secondTaskReads >= 2, "当前任务状态必须约每五秒刷新");
    await ui.getByRole("button", { name: "切换任务", exact: true }).click();
    await picker.getByRole("combobox", { name: "筛选日志任务状态" }).focus();
    await picker.getByRole("combobox", { name: "筛选日志任务状态" }).press("Enter");
    await ui.getByRole("option", { name: "已停止", exact: true }).click();
    await picker.getByText("模拟采集", { exact: true }).click();
    await picker.getByRole("button", { name: "取消", exact: true }).click();
    await picker.waitFor({ state: "hidden" });
    assert.equal(await ui.getByText("备用采集", { exact: true }).count(), 1, "取消不能改变当前任务");
    await ui.getByRole("button", { name: "切换任务", exact: true }).click();
    await picker.getByRole("textbox", { name: "搜索日志任务" }).fill("模拟");
    await picker.getByRole("button", { name: "搜索日志任务" }).click();
    await picker.getByText("模拟采集", { exact: true }).click();
    await picker.getByRole("button", { name: "确认切换", exact: true }).click();
    await ui.getByRole("button", { name: "切换任务", exact: true }).click();
    await picker.getByRole("textbox", { name: "搜索日志任务" }).fill("备用");
    await picker.getByRole("button", { name: "搜索日志任务" }).click();
    await picker.getByText("备用采集", { exact: true }).click();
    await picker.getByRole("button", { name: "确认切换", exact: true }).click();
    await ui.waitForTimeout(1100);
    assert.equal(await ui.getByText("模拟采集", { exact: true }).count(), 0, "旧任务迟到响应不能覆盖新任务信息栏");
    await ui.getByRole("button", { name: "切换任务", exact: true }).click();
    await picker.getByRole("combobox", { name: "筛选日志任务状态" }).focus();
    await picker.getByRole("combobox", { name: "筛选日志任务状态" }).press("Enter");
    await ui.getByRole("option", { name: "采集失败", exact: true }).click();
    await picker.getByText("没有符合条件的任务", { exact: true }).waitFor();
    assert.equal(await picker.getByRole("button", { name: "确认切换", exact: true }).isDisabled(), true, "空结果不能确认切换");
    await picker.getByRole("button", { name: "取消", exact: true }).click();
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
      assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= width + 1, `抽屉必须完整位于视口内：${JSON.stringify({ width, bounds })}`);
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
    assert.equal(await ui.getByRole("button", { name: "编辑资源", exact: true }).count(), 0, "非创建者不能编辑他人资源");
    await ui.getByRole("tab", { name: "采集任务", exact: true }).click();
    await ui.locator(".data-table .el-table__row").first().waitFor();
    assert.equal(await ui.getByRole("button", { name: "编辑任务", exact: true }).count(), 0, "非创建者不能编辑他人任务");
    allowCreate = true;
    await ui.reload();
    await ui.locator(".resource-table .el-table__row").first().waitFor();
    assert.equal(await ui.getByRole("button", { name: "新建采集任务", exact: true }).count(), 2, "创建任务不依赖资源所有者");
    assert.deepEqual(errors, []);
    await context.close();
  }
} finally {
  await browser.close();
}
