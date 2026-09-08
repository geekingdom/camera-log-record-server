// 前端唯一入口：在此统一注册 Element Plus，并加载全局样式后挂载根应用。
import { createApp } from "vue";
import ElementPlus from "element-plus";
import zhCn from "element-plus/es/locale/lang/zh-cn";
import "element-plus/dist/index.css";
import "./shared/styles.css";
import App from "./app/App.vue";
createApp(App).use(ElementPlus, { locale: zhCn }).mount("#app");
