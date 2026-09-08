<script setup lang="ts">
// 只读状态详情直接使用列表最新快照，不另建连接，也不推断设备是否已解锁。
import type { Task } from "../../shared/types";
import { taskStatusLabel, taskStatusTone } from "./taskStatus";

defineProps<{ task?: Task }>();
const open = defineModel<boolean>({ default: false });
const phases: Record<string, string> = {
  STARTED: "开始切换", CHALLENGE_RECEIVED: "已获取密文",
  ASH_READY: "ASH 已就绪", ALREADY_ASH: "已处于 ASH", FAILED: "切换失败",
  RECOVERED: "调试失败，普通命令已恢复", BLOCKED: "调试失败，命令通道待恢复",
};

function timestamp(value?: string) {
  if (!value) return "未记录";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}
</script>

<template>
  <el-dialog v-model="open" title="任务运行状态" width="min(640px, calc(100% - 32px))" destroy-on-close>
    <div v-if="task" class="task-diagnostics">
      <div class="diagnostic-heading">
        <h2>{{ task.name }}</h2>
        <el-tag :type="taskStatusTone(task.status)">{{ taskStatusLabel(task.status) }}</el-tag>
      </div>
      <dl>
        <dt>连接地址</dt><dd>{{ task.ip }}:{{ task.port }}</dd>
        <dt>任务 ID</dt><dd>{{ task.id }}</dd>
        <dt>运行 ID</dt><dd>{{ task.runId || "未建立" }}</dd>
        <dt>会话 ID</dt><dd>{{ task.sessionId || "未建立" }}</dd>
        <dt>设备模式</dt><dd>{{ task.shellMode && task.shellMode !== "UNKNOWN" ? task.shellMode : "尚未确认" }}</dd>
        <dt>调试阶段</dt><dd>{{ phases[task.debugPhase ?? ""] ?? task.debugPhase ?? "未执行" }}</dd>
        <dt>命令通道</dt><dd>{{ task.commandBlocked ? '暂不可发送，日志采集继续' : '未阻断' }}</dd>
        <dt>状态更新时间</dt><dd>{{ timestamp(task.updatedAt) }}</dd>
      </dl>
      <section class="diagnostic-error" :class="{ 'has-error': Boolean(task.error) }">
        <h3>最近错误记录</h3>
        <p>{{ task.error || task.debugError || "无错误记录" }}</p>
      </section>
    </div>
    <p v-else>当前列表中已无此任务</p>
    <template #footer><el-button @click="open = false">关闭</el-button></template>
  </el-dialog>
</template>

<style scoped>
.task-diagnostics { min-width: 0; }
.diagnostic-heading { display: flex; align-items: start; gap: 12px; justify-content: space-between; }
.diagnostic-heading h2 { min-width: 0; overflow-wrap: anywhere; }
.diagnostic-heading .el-tag { flex-shrink: 0; }
dl { display: grid; grid-template-columns: 108px minmax(0, 1fr); gap: 12px; margin: 24px 0; }
dt { color: #667085; }
dd { margin: 0; overflow-wrap: anywhere; }
.diagnostic-error { border-top: 1px solid #e4e7ec; padding-top: 16px; }
.diagnostic-error h3 { margin: 0 0 8px; }
.diagnostic-error p { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.6; }
.diagnostic-error.has-error p { color: #b42318; }
@media (max-width: 480px) {
  dl { grid-template-columns: minmax(0, 1fr); gap: 5px; }
  dd { margin-bottom: 9px; }
}
</style>
