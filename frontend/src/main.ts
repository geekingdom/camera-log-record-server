// 前端唯一入口：组件由构建插件按需引入；服务和 loading 指令样式在此显式保留。
import { createApp } from "vue";
import "element-plus/es/components/loading/style/css";
import "element-plus/es/components/message/style/css";
import "element-plus/es/components/message-box/style/css";
import "./shared/styles.css";
import App from "./app/App.vue";
createApp(App).mount("#app");
