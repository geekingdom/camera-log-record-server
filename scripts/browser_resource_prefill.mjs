// 全部业务接口由浏览器模拟，验证懒加载编辑器首次打开的回填与只读身份字段。
import assert from 'node:assert/strict';
import { mkdir } from 'node:fs/promises';
const playwrightModule = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const { chromium } = playwrightModule.default || playwrightModule;
const browser = await chromium.launch({ headless: true, channel: 'chrome' });
const resource = { id: 'prefill', name: '资源回填验证', kind: 'HIKVISION_NETWORK', ip: '192.0.2.34',
  username: 'device-user', authType: 'BASIC', model: '', subSerialNumber: '', version: 7, createdBy: 'admin',
  healthStatus: 'ONLINE', healthCheckedAt: '2026-09-10T04:00:00Z' };
const serialResource = { id: 'serial', name: '串口资源', kind: 'SERIAL_SERVER', ip: '192.0.2.35', version: 1, createdBy: 'admin' };
const paged = items => ({ items, total: items.length, page: 1, pageSize: 20 });
try {
  for (const width of [1440, 390]) {
    const context = await browser.newContext({ viewport: { width, height: 960 } });
    await context.route('**/api/v1/**', async route => {
      const path = new URL(route.request().url()).pathname;
      assert.equal(route.request().method(), 'GET', '验收不得修改真实或模拟资源');
      let body;
      if (path === '/api/v1/auth/me') body = { user: { id: 'admin', username: 'admin', isAdmin: true,
        enabled: true, scopes: ['*'], mustChangePassword: false } };
      else if (path === '/api/v1/resources') body = paged([resource, serialResource]);
      else if (path === '/api/v1/resources/prefill') body = resource;
      else body = paged([]);
      await route.fulfill({ json: body });
    });
    const page = await context.newPage();
    page.setDefaultTimeout(10_000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(process.env.BROWSER_BASE_URL || 'http://127.0.0.1:5173', { waitUntil: 'domcontentloaded', timeout: 10_000 });
    await page.getByText('当前连通', { exact: true }).waitFor();
    const serialRow = page.getByRole('row').filter({ hasText: serialResource.name });
    assert.equal(await serialRow.getByText('当前连通', { exact: true }).count(), 0, '串口资源不应显示海康认证状态');
    await page.getByLabel('编辑资源').first().click();
    const drawer = page.locator('.el-drawer').last();
    await page.getByLabel('资源名称', { exact: true }).waitFor();
    await page.waitForFunction(() => document.querySelector('.resource-editor-drawer input')?.value === '资源回填验证');
    await page.waitForTimeout(350);
    const stable = await drawer.evaluate(async element => {
      const first = element.getBoundingClientRect();
      await new Promise(resolve => requestAnimationFrame(resolve));
      const second = element.getBoundingClientRect();
      return first.x === second.x && first.width === second.width && second.width > 0;
    });
    assert.equal(stable, true, '编辑抽屉动画尚未稳定');
    assert.equal(await page.getByLabel('资源名称', { exact: true }).inputValue(), resource.name);
    assert.equal(await page.getByLabel('IP 地址', { exact: true }).inputValue(), resource.ip);
    assert.equal(await page.getByLabel('IP 地址', { exact: true }).isDisabled(), true);
    assert.equal(await drawer.locator('input').filter({ visible: true }).count() > 0, true);
    assert.equal(await drawer.getByLabel('用户名', { exact: true }).inputValue(), resource.username);
    assert.equal(await drawer.getByLabel('密码（留空保持原值）', { exact: true }).inputValue(), '');
    const drawerWidth = await drawer.evaluate(element => element.getBoundingClientRect().width);
    assert.ok(drawerWidth <= width + 1, '编辑抽屉超过当前视口');
    assert.deepEqual(errors, []);
    await mkdir('output/playwright', { recursive: true });
    await page.screenshot({ path: `output/playwright/resource-prefill-${width}.png`, fullPage: true });
    await context.close();
  }
  console.log('资源编辑首次回填：桌面与手机通过');
} finally { await browser.close(); }
