// 开发代理保留正式 API 路径，并代理同一路径下的实时 WebSocket 连接。
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import Components from "unplugin-vue-components/vite";
import { ElementPlusResolver } from "unplugin-vue-components/resolvers";
import { existsSync, readdirSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";

const require = createRequire(import.meta.url);
const componentRoot = join(dirname(require.resolve("element-plus/package.json")), "es/components");
const componentStyles = readdirSync(componentRoot)
  .filter(name => existsSync(join(componentRoot, name, "style/css.mjs")))
  .map(name => `element-plus/es/components/${name}/style/css`);

export default defineConfig({
  plugins: [
    vue(),
    Components({
      dts: false,
      directives: true,
      resolvers: [ElementPlusResolver({ importStyle: "css" })],
    }),
  ],
  // 懒加载页面的样式由组件插件注入，提前预优化以免首次导航触发整页刷新。
  optimizeDeps: {
    include: ["element-plus/es", ...componentStyles],
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          // 资源趋势弹窗本身是异步组件；图表层和其 Canvas 渲染依赖均只在打开弹窗后
          // 才解析。按稳定库边界拆分可降低单块体积，且不会让主入口预加载图表依赖。
          if (id.includes("/node_modules/zrender/")) return "zrender";
          if (id.includes("/node_modules/echarts/")) return "echarts";
          return undefined;
        },
      },
    },
  },
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", ws: true },
    },
  },
});
