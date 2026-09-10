// 工作区导航元数据独立于根协调逻辑，权限过滤仍由 App 根据当前会话执行。
import {
  BookOpen,
  FileCode2,
  HardDrive,
  Radio,
  ScrollText,
  Server,
  Settings2,
  ShieldCheck,
  Terminal,
} from "lucide-vue-next";

export const workspaceNavigation = [
  { key: "resources", label: "设备资源", icon: HardDrive, scope: "tasks:read" },
  { key: "tasks", label: "采集任务", icon: Radio, scope: "tasks:read" },
  { key: "logs", label: "日志工作台", icon: Terminal, scope: "logs:read" },
  { key: "templates", label: "命令模板", icon: FileCode2, scope: "templates:read" },
  { key: "nodes", label: "服务节点", icon: Server, admin: true },
  { key: "access", label: "账号管理", icon: ShieldCheck, scope: "service-tokens:read" },
  { key: "audit", label: "审计与事件", icon: ScrollText, admin: true },
  { key: "settings", label: "后台配置", icon: Settings2, admin: true },
  { key: "api-reference", label: "API 文档", icon: BookOpen },
];
