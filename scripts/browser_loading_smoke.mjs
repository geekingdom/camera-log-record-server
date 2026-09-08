// 生产分块验收：仅访问页面和填写未保存草稿，不提交设备认证或采集命令。
import assert from "node:assert/strict";
process.loadEnvFile(".env");
const imported = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const { chromium } = imported.default || imported;
const baseUrl = process.env.BROWSER_BASE_URL || "http://127.0.0.1:4173";
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const context = await browser.newContext();
await context.addInitScript(token => sessionStorage.setItem("camera-log-record-token", token), process.env.BOOTSTRAP_TOKEN);
const page = await context.newPage();
page.setDefaultTimeout(10000);
const scripts = [];
const mutations = [];
page.on("request", request => {
  if (request.resourceType() === "script") scripts.push(new URL(request.url()).pathname);
  if (new URL(request.url()).pathname.startsWith("/api/") && request.method() !== "GET") mutations.push(request.method());
});
try {
  await page.goto(baseUrl);
  await page.getByRole("button", { name: "新建资源", exact: true }).waitFor();
  assert(scripts.some(path => /ResourceWorkspace-.*\.js$/.test(path)), "必须针对生产分块运行");
  assert(!scripts.some(path => /(?:AuditWorkspace|TaskEditor|ResourceEditor)-.*\.js$/.test(path)), "首屏不应加载未访问模块");
  await page.getByRole("button", { name: "新建资源", exact: true }).click();
  await page.getByLabel("资源名称", { exact: true }).fill("未保存的刷新回归草稿");
  // 等待真实的五秒轮询响应，证明业务刷新不会卸载已打开的编辑器。
  await page.waitForResponse(response => new URL(response.url()).pathname === "/api/v1/resources" && response.request().method() === "GET");
  assert.equal(await page.getByLabel("资源名称", { exact: true }).inputValue(), "未保存的刷新回归草稿");
  assert.equal(await page.locator(".async-view-overlay").count(), 0, "加载结束不能残留遮罩");
  await page.getByRole("button", { name: "关闭", exact: true }).click();

  // 阻断尚未请求的业务分块；随后恢复网络并通过明确的整页刷新退出 ESM 失败缓存。
  let blocked = false;
  await page.route("**/assets/AuditWorkspace-*.js", route => { blocked = true; return route.abort(); });
  await page.getByRole("tab", { name: "审计与事件", exact: true }).click();
  await page.getByTestId("async-view-error").waitFor();
  assert(blocked, "未实际阻断分块，不能声称失败恢复通过");
  await page.unroute("**/assets/AuditWorkspace-*.js");
  await Promise.all([
    page.waitForEvent("load"),
    page.getByTestId("async-view-retry").click(),
  ]);
  await page.getByRole("button", { name: "新建资源", exact: true }).waitFor();
  await page.getByRole("tab", { name: "审计与事件", exact: true }).click();
  await page.locator(".audit-workspace").waitFor();
  assert.equal(await page.getByTestId("async-view-error").count(), 0);
  assert.deepEqual(mutations, [], "加载恢复不能提交业务写操作");
  console.log(JSON.stringify({ passed: true, lazyModulesVerified: true, draftPreserved: true, failureRecovery: true, mutations: 0 }));
} finally {
  await browser.close();
}
