# 控制台按需加载

控制台保留 Vue3 与 Element Plus，业务页面在首次访问时加载，任务、模板、资源编辑器与任务诊断在打开时加载。Element Plus 使用 `unplugin-vue-components` 的组件解析器按实际模板引用导入，根配置提供中文 locale，消息、确认弹窗与 loading 样式显式保留。

`shared/AsyncView.vue` 负责加载状态、失败状态和过期加载结果隔离。列表刷新转发给业务组件暴露的 `reload`，不重新挂载页面，资源页五秒轮询不能清空用户草稿。编辑器加载中使用视口遮罩，加载成功立即解除，让正常抽屉管理焦点与层级。分块加载失败后由用户点击重新加载页面，避免浏览器 ESM 失败缓存；不会自动重放保存、认证或命令操作，整页刷新会丢弃尚未保存的页面状态。

## 生产构建验证

2026-09-09 本机 `npm run build` 的主入口 JavaScript 从 1,148.13 kB（gzip 370.72 kB）降到 320.61 kB（gzip 113.94 kB）。该数值仅指入口文件，不能当作所有页面总下载量或加载耗时。日期控件、表格及业务模块分配到共享或页面分块，构建不再出现单个分块超过 500 kB 的提示。

运行生产预览后使用已有浏览器验收工具：

```sh
cd frontend
npm run build
npm exec vite -- preview --host 127.0.0.1 --port 4173 --strictPort
```

在仓库根目录执行，Playwright 模块位置可通过 `PLAYWRIGHT_MODULE` 配置：

```sh
BROWSER_BASE_URL=http://127.0.0.1:4173 BROWSER_SMOKE_TASK_ID=<合成验收任务ID> BROWSER_SCREENSHOTS=output/playwright-production node scripts/browser_smoke.mjs
BROWSER_BASE_URL=http://127.0.0.1:4173 node scripts/browser_loading_smoke.mjs
```

完整验收覆盖资源、任务、模板草稿、实时打印、小时下载和搜索，检查桌面与移动端布局。脚本必须传入名称含“合成”“协议压测”或“容器验收”等标识的 `BROWSER_SMOKE_TASK_ID`，拒绝扫描或操作真实采集任务；它会创建首登改密的临时操作员账号，并在结束时删除账号和模板。管理员菜单由 `browser_user_auth.mjs` 的路由替身验收。加载专用验收确认首屏不请求未访问的业务分块、资源轮询保留未保存草稿、加载后无残留遮罩，以及中断审计分块后通过用户重载恢复。专用验收不提交任何业务写操作。两项生产浏览器验收和前端 25 项单元测试均已通过；生产完整流程控制台错误为零，已检查桌面日志页与移动抽屉截图。
