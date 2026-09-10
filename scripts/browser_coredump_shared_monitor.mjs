// 同资源 Coredump 负责人编辑器验收：全部 API 由浏览器路由模拟，不连接设备或 NFS。
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const baseUrl = process.env.BASE_URL || process.env.BROWSER_BASE_URL || "http://127.0.0.1:5173";
const screenshots = process.env.BROWSER_SCREENSHOTS || "output/playwright";
const resources = [
  { id: "camera-a", name: "模拟海康 A", kind: "HIKVISION_NETWORK", ip: "192.0.2.10", version: 1 },
  { id: "camera-b", name: "模拟海康 B", kind: "HIKVISION_NETWORK", ip: "192.0.2.11", version: 1 },
];
const ownTask = { id: "owner-self", name: "本人负责采集", resourceId: "camera-a", protocol: "SSH", ip: "192.0.2.10", port: 22,
  username: "fixture", enableCoredumpMonitor: true, status: "COLLECTING", desiredState: "RUNNING", version: 1, initialCommands: [], scheduledCommands: [] };
const pageOf = items => ({ items, total: items.length, page: 1, pageSize: 100 });

function monitor(active, ownerTask, mountStatus = null) { return { active, ownerTask, mountStatus }; }
async function assertViewport(page, width, label) {
  const geometry = await page.evaluate(() => ({ scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth }));
  assert.ok(geometry.scrollWidth <= geometry.clientWidth + 1, `${label} 在 ${width}px 出现横向溢出：${JSON.stringify(geometry)}`);
}

const browser = await chromium.launch({ headless: true, channel: "chrome" });
try {
  await mkdir(screenshots, { recursive: true });
  for (const width of [1440, 390]) {
    let cameraAReads = 0;
    let cameraBFailure = false;
    const created = [];
    const context = await browser.newContext({ viewport: { width, height: 900 } });
    await context.route("**/api/v1/**", async route => {
      const request = route.request(), url = new URL(request.url()), path = url.pathname;
      if (path === "/api/v1/auth/me") return route.fulfill({ json: { user: { id: "admin", username: "admin", displayName: "验收管理员", isAdmin: true, enabled: true, mustChangePassword: false, scopes: ["*"] } } });
      if (path === "/api/v1/resources") return route.fulfill({ json: pageOf(resources) });
      if (path === "/api/v1/resources/camera-a/coredump-monitor") {
        cameraAReads += 1;
        return route.fulfill({ json: cameraAReads <= 2
          ? monitor(true, { id: "owner-other", name: "另一采集任务" }, "MOUNTED")
          : cameraAReads === 3
            ? monitor(true, { id: "owner-self", name: ownTask.name }, "MOUNTED")
            : monitor(false, null) });
      }
      if (path === "/api/v1/resources/camera-b/coredump-monitor") {
        if (cameraBFailure) return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ message: "模拟查询失败" }) });
        return route.fulfill({ json: monitor(true, { id: "owner-b", name: "B 资源负责人" }, "MOUNTED") });
      }
      if (path === "/api/v1/tasks" && request.method() === "POST") {
        created.push(request.postDataJSON());
        return route.fulfill({ json: { ...ownTask, ...created.at(-1), id: `new-${created.length}` } });
      }
      if (path === "/api/v1/tasks") return route.fulfill({ json: pageOf([ownTask]) });
      if (path === "/api/v1/tasks/owner-self") return route.fulfill({ json: ownTask });
      if (path === "/api/v1/command-templates") return route.fulfill({ json: pageOf([]) });
      return route.fulfill({ json: pageOf([]) });
    });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(baseUrl, { waitUntil: "networkidle" });
    await page.getByRole("tab", { name: "采集任务", exact: true }).click();
    await page.getByRole("button", { name: "新建任务", exact: true }).click();
    const drawer = page.getByRole("dialog", { name: "新建采集任务", exact: true });
    await drawer.waitFor();
    const resourceSelect = drawer.locator(".el-form-item").filter({ hasText: "设备资源" }).locator(".el-select");
    await resourceSelect.click();
    await page.getByRole("option", { name: /模拟海康 A/ }).click();
    const monitorItem = drawer.locator(".el-form-item").filter({ hasText: "Coredump 监控" });
    await monitorItem.getByText("另一采集任务", { exact: false }).waitFor();
    const sharedSwitch = monitorItem.locator(".el-switch");
    assert.ok((await sharedSwitch.getAttribute("class")).includes("is-disabled"), "新任务必须只读展示同资源负责人");
    assert.ok((await sharedSwitch.getAttribute("class")).includes("is-checked"), "共享负责人存在时开关必须显示开启");
    await page.getByRole("option", { name: /模拟海康 A/ }).waitFor({ state: "hidden" });
    await monitorItem.scrollIntoViewIfNeeded();
    await monitorItem.evaluate(async element => {
      const first = element.getBoundingClientRect();
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      const second = element.getBoundingClientRect();
      if (first.x !== second.x || first.y !== second.y || first.width !== second.width) throw new Error("Coredump 共享状态布局尚未稳定");
    });
    assert.ok(await sharedSwitch.isVisible(), "共享开启开关必须出现在截图视口中");
    assert.ok(await monitorItem.getByText("另一采集任务", { exact: false }).isVisible(), "负责人说明必须出现在截图视口中");
    await page.screenshot({ path: `${screenshots}/coredump-shared-owner-${width}.png`, fullPage: true });
    await drawer.getByLabel("任务名称", { exact: true }).fill("共享状态新任务");
    await drawer.getByLabel("用户名", { exact: true }).fill("fixture");
    await drawer.getByLabel("密码", { exact: true }).fill("fixture-password");
    await drawer.getByRole("button", { name: "保存任务", exact: true }).click();
    await drawer.waitFor({ state: "hidden" });
    assert.equal(created.at(-1).enableCoredumpMonitor, false, "共享显示不得保存为新任务启用状态");

    await page.getByRole("row").filter({ hasText: ownTask.name }).getByRole("button", { name: "编辑任务" }).click();
    const ownDrawer = page.getByRole("dialog", { name: `任务 · ${ownTask.name}`, exact: true });
    await ownDrawer.waitFor();
    // 编辑本人负责的任务时，状态接口返回同一任务，原开关保留可编辑性。
    const ownSwitch = ownDrawer.locator(".el-form-item").filter({ hasText: "Coredump 监控" }).locator(".el-switch");
    await ownDrawer.locator(".inline-option").filter({ hasText: ownTask.name }).waitFor();
    assert.ok(!(await ownSwitch.getAttribute("class")).includes("is-disabled"), "本人负责的任务必须可编辑原开关");
    await ownDrawer.getByRole("button", { name: "关闭", exact: true }).click();

    // 重新打开新建表单，验证资源和协议切换会使先前负责人状态失效，而非迟到覆盖。
    await page.getByRole("button", { name: "新建任务", exact: true }).click();
    const switchingDrawer = page.getByRole("dialog", { name: "新建采集任务", exact: true });
    const switchingResourceSelect = switchingDrawer.locator(".el-form-item").filter({ hasText: "设备资源" }).locator(".el-select");
    await switchingResourceSelect.click();
    await page.getByRole("option", { name: /模拟海康 A/ }).click();
    const switchingItem = switchingDrawer.locator(".el-form-item").filter({ hasText: "Coredump 监控" });
    await switchingItem.getByText("当前没有正在负责 Coredump NFS 挂载监控的任务", { exact: true }).waitFor();
    await switchingItem.locator(".el-switch").click();
    // 草稿已主动打开时切换到共享负责人资源，提交仍必须清除本任务配置。
    await switchingResourceSelect.click();
    await page.getByRole("option", { name: /模拟海康 B/ }).click();
    await switchingItem.getByText("B 资源负责人", { exact: false }).waitFor();
    assert.ok((await switchingItem.locator(".el-switch").getAttribute("class")).includes("is-disabled"), "切换到共享负责人后必须锁定展示开关");
    await switchingDrawer.getByLabel("任务名称", { exact: true }).fill("草稿开启后切换共享资源");
    await switchingDrawer.getByLabel("用户名", { exact: true }).fill("fixture");
    await switchingDrawer.getByLabel("密码", { exact: true }).fill("fixture-password");
    await switchingDrawer.getByRole("button", { name: "保存任务", exact: true }).click();
    await switchingDrawer.waitFor({ state: "hidden" });
    assert.equal(created.at(-1).enableCoredumpMonitor, false, "已开启草稿切换共享资源后不得提交 true");

    await page.getByRole("button", { name: "新建任务", exact: true }).click();
    const transitionDrawer = page.getByRole("dialog", { name: "新建采集任务", exact: true });
    const transitionResourceSelect = transitionDrawer.locator(".el-form-item").filter({ hasText: "设备资源" }).locator(".el-select");
    await transitionResourceSelect.click();
    await page.getByRole("option", { name: /模拟海康 B/ }).click();
    const transitionItem = transitionDrawer.locator(".el-form-item").filter({ hasText: "Coredump 监控" });
    await transitionItem.getByText("B 资源负责人", { exact: false }).waitFor();
    await transitionDrawer.locator(".el-form-item").filter({ hasText: "连接协议" }).locator(".el-select").click();
    await page.getByRole("option", { name: "Telnet 设备", exact: true }).click();
    assert.equal(await transitionItem.count(), 0, "切换非 SSH 协议必须移除共享 Coredump 展示");
    await transitionDrawer.locator(".el-form-item").filter({ hasText: "连接协议" }).locator(".el-select").click();
    await page.getByRole("option", { name: "SSH", exact: true }).click();
    cameraBFailure = true;
    await transitionResourceSelect.click();
    await page.getByRole("option", { name: /模拟海康 A/ }).click();
    await page.waitForTimeout(5_200);
    await transitionDrawer.getByText("当前没有正在负责 Coredump NFS 挂载监控的任务", { exact: true }).waitFor();
    await transitionResourceSelect.click();
    await page.getByRole("option", { name: /模拟海康 B/ }).click();
    await transitionDrawer.getByText("无法确认共享 Coredump 监控状态", { exact: true }).waitFor();
    await assertViewport(page, width, "Coredump 负责人编辑器");
    assert.deepEqual(errors, []);
    await context.close();
  }
  console.log("共享 Coredump 负责人浏览器验收通过，已检查 1440px 与 390px 截图");
} finally {
  await browser.close();
}
