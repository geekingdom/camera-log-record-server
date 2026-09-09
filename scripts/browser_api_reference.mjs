// 内置 API 文档浏览器验收：catalog 来自真实后端，仅在浏览器层模拟读取，不执行示例接口。
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdir } from "node:fs/promises";

const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const python = process.env.PYTHON_BIN || ".venv/bin/python";
const catalog = JSON.parse(execFileSync(python, ["-c", "import json; from camera_logs.main import create_app; from camera_logs.reference.catalog import catalog; print(json.dumps(catalog(create_app())))"], { encoding: "utf8" }));
const screenshots = process.env.BROWSER_SCREENSHOTS || "output/playwright";
const browser = await chromium.launch({ headless: true, channel: "chrome" });

await mkdir(screenshots, { recursive: true });
try {
  for (const [width, height] of [[1440, 900], [390, 844], [320, 760]]) {
    let referenceRequests = 0;
    let adminMode = false;
    const context = await browser.newContext({ viewport: { width, height } });
    await context.route("**/api/v1/**", async route => {
      const path = new URL(route.request().url()).pathname;
      if (path === "/api/v1/auth/me") {
        await route.fulfill({ contentType: "application/json", body: JSON.stringify({
          user: adminMode
            ? { id: "admin", username: "admin", displayName: "文档管理员", scopes: ["*"], resourceIds: null, isAdmin: true, mustChangePassword: false }
            : { id: "fixture", username: "fixture", displayName: "文档验收", scopes: ["tasks:read"], resourceIds: null, isAdmin: false, mustChangePassword: false },
        }) });
        return;
      }
      if (path === "/api/v1/api-reference") {
        referenceRequests += 1;
        if (referenceRequests === 1) {
          await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ error: { message: "目录暂不可用" } }) });
          return;
        }
        await route.fulfill({ contentType: "application/json", body: JSON.stringify(catalog) });
        return;
      }
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ items: [], total: 0, page: 1, pageSize: 20 }) });
    });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(process.env.BASE_URL || "http://127.0.0.1:5173");
    assert.equal(await page.getByRole("tab", { name: "后台配置", exact: true }).count(), 0, "普通用户不应看到后台配置入口");
    assert.equal(await page.getByRole("tab", { name: "API 文档", exact: true }).count(), 1, "普通用户必须能访问 API 文档");
    adminMode = true;
    await page.reload();
    const administratorTabs = await page.getByRole("tablist", { name: "工作空间导航" }).getByRole("tab").allTextContents();
    const settingsIndex = administratorTabs.indexOf("后台配置");
    assert.equal(administratorTabs[settingsIndex + 1], "API 文档", "管理员导航中 API 文档必须紧随后台配置");
    await page.screenshot({ path: `${screenshots}/api-reference-navigation-admin-${width}.png`, fullPage: true, animations: "disabled" });
    adminMode = false;
    await page.reload();
    await page.getByRole("tab", { name: "API 文档", exact: true }).click();
    await page.getByRole("alert").waitFor();
    await page.getByRole("button", { name: "重试", exact: true }).click();
    await page.locator(".api-reference-grid").waitFor();
    assert.equal(referenceRequests, 2, "加载失败后必须仅重试接口目录读取");

    const search = page.getByRole("searchbox", { name: "搜索接口目录" });
    await search.fill("resources");
    await page.locator(".api-operation-list > button").first().click();
    await page.locator(".parameter-row").first().waitFor();
    await page.locator(".api-code-section pre").nth(0).waitFor();
    await page.locator(".api-code-section pre").nth(1).waitFor();
    await page.locator(".api-guides").getByText("第三方鉴权", { exact: true }).waitFor();
    await page.waitForTimeout(350);
    const metrics = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
      requestBlocks: document.querySelectorAll(".api-code-section pre").length,
      parameters: document.querySelectorAll(".parameter-row").length,
      guides: document.querySelectorAll(".api-guides section").length,
      overflow: [...document.querySelectorAll('body *')].filter(element => {
        if (element.getBoundingClientRect().right <= document.documentElement.clientWidth + 1) return false;
        let parent = element.parentElement;
        while (parent && parent !== document.body) {
          if (getComputedStyle(parent).overflowX !== 'visible' && parent.getBoundingClientRect().right <= document.documentElement.clientWidth + 1) return false;
          parent = parent.parentElement;
        }
        return true;
      }).slice(0, 8).map(element => ({ tag: element.tagName, class: element.className, width: element.getBoundingClientRect().width })),
      roots: [...document.querySelectorAll('html,body,.workspace,.api-reference,.api-reference-grid,.api-guides,.api-operation-detail,.api-reference-intro')].map(e=>({class:e.className,sw:e.scrollWidth,cw:e.clientWidth,width:e.getBoundingClientRect().width,right:e.getBoundingClientRect().right})),
    }));
    await page.screenshot({ path: `${screenshots}/api-reference-${width}.png`, fullPage: true, animations: "disabled" });
    assert.ok(metrics.scrollWidth <= metrics.clientWidth + 1, `页面不应横向溢出：${JSON.stringify({ width, ...metrics })}`);
    assert.ok(metrics.parameters > 0, "资源目录必须展示查询参数");
    assert.equal(metrics.requestBlocks, 2, "详情必须展示请求和响应示例");
    assert.ok(metrics.guides > 1, "详情页必须展示接入指南");
    await search.fill("/logs");
    await page.locator(".api-operation-list > button").filter({ has: page.locator(".http-method.ws") }).click();
    const realtimeExample = await page.locator(".api-code-section pre").first().innerText();
    assert.ok(realtimeExample.includes("ws://"), "HTTP 实时示例必须支持 ws");
    assert.ok(realtimeExample.includes("wss://"), "HTTPS 实时示例必须支持 wss");
    const service = new URL(page.url());
    assert.ok(realtimeExample.includes(`${service.protocol === "https:" ? "wss:" : "ws:"}//${service.host}/api/v1/tasks/`), "当前部署示例必须保留协议和端口");
    await page.screenshot({ path: `${screenshots}/api-reference-${width}.png`, fullPage: true, animations: "disabled" });
    assert.deepEqual(errors, []);
    await context.close();
  }
  console.log(JSON.stringify({ passed: true, screenshots: `${screenshots}/api-reference-{1440,390,320}.png`, catalogOperations: catalog.operations.length }));
} finally {
  await browser.close();
}
