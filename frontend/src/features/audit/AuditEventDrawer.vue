<script setup lang="ts">
// 事件详情抽屉优先呈现可操作的排障字段，原始服务端脱敏 JSON 仅作补充核对。
import { computed } from "vue";
import { CircleAlert, Copy, Fingerprint, Network, Timer } from "lucide-vue-next";
import { ElMessage } from "element-plus";
import type { EventBase } from "./api";

const props = defineProps<{
  row?: EventBase;
  title: string;
}>();
const open = defineModel<boolean>({ default: false });

const timestamp = computed(() => formatTime(props.row?.createdAt ?? props.row?.detectedAt));
const raw = computed(() => JSON.stringify(props.row, null, 2));
const hasReason = computed(() => Boolean(props.row?.reason));

function value(input: unknown) {
  return input === undefined || input === null || input === "" ? "-" : String(input);
}

function formatTime(input: unknown) {
  if (typeof input !== "string") return "-";
  const parsed = new Date(input);
  return Number.isNaN(parsed.getTime()) ? input : parsed.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" });
}

function levelLabel(input?: string) {
  return ({ INFO: "信息", WARNING: "警告", ERROR: "错误" } as Record<string, string>)[input ?? ""] ?? "未记录";
}

function outcomeLabel(input?: string) {
  return ({ SUCCEEDED: "成功", FAILED: "失败", PENDING: "处理中", CANCELLED: "已取消", UNKNOWN: "未知" } as Record<string, string>)[input ?? ""] ?? "未记录";
}

async function copyRequestId() {
  const requestId = props.row?.requestId;
  if (!requestId) return;
  try {
    await navigator.clipboard.writeText(requestId);
    ElMessage.success("请求编号已复制");
  } catch {
    ElMessage.error("无法复制请求编号");
  }
}
</script>

<template>
  <el-drawer v-model="open" :title="title" direction="rtl" size="min(540px, 94vw)" class="audit-event-drawer">
    <template v-if="row">
      <section class="drawer-summary">
        <div>
          <span class="drawer-eyebrow">事件结论</span>
          <h3>{{ row.summary || "未提供事件摘要" }}</h3>
        </div>
        <div class="drawer-tags">
          <el-tooltip v-if="row.level" :content="row.level"><el-tag :type="row.level === 'ERROR' ? 'danger' : row.level === 'WARNING' ? 'warning' : 'success'" effect="plain">{{ levelLabel(row.level) }}</el-tag></el-tooltip>
          <el-tooltip v-if="row.outcome" :content="row.outcome"><el-tag effect="plain">{{ outcomeLabel(row.outcome) }}</el-tag></el-tooltip>
        </div>
      </section>

      <el-alert v-if="hasReason" :title="String(row.reason)" type="error" :closable="false" show-icon class="reason-alert" />

      <section class="detail-section">
        <h4><Fingerprint :size="16" /> 关联对象</h4>
        <dl class="detail-grid">
          <dt>任务</dt><dd>{{ value(row.taskName || row.taskId) }}</dd>
          <dt>设备</dt><dd>{{ value(row.deviceIp) }}</dd>
          <dt>运行</dt><dd>{{ value(row.runId) }}</dd>
          <dt>会话</dt><dd>{{ value(row.sessionId) }}</dd>
          <dt>节点</dt><dd>{{ value(row.nodeId) }}</dd>
          <dt>对象</dt><dd>{{ value(row.targetName || row.targetId) }}</dd>
        </dl>
      </section>

      <section class="detail-section">
        <h4><Network :size="16" /> 请求上下文</h4>
        <dl class="detail-grid">
          <dt>操作者</dt><dd>{{ value(row.actorName || row.actor) }}</dd>
          <dt>来源 IP</dt><dd>{{ value(row.clientIp) }}</dd>
          <dt>路由</dt><dd>{{ value(row.route) }}</dd>
          <dt>方法</dt><dd>{{ value(row.method) }}</dd>
          <dt>状态</dt><dd>{{ value(row.httpStatus ?? row.status) }}</dd>
          <dt>耗时</dt><dd>{{ row.durationMs === undefined ? "-" : `${row.durationMs} ms` }}</dd>
        </dl>
      </section>

      <section class="detail-section">
        <h4><Timer :size="16" /> 时间与追踪</h4>
        <dl class="detail-grid">
          <dt>发生时间</dt><dd>{{ timestamp }}</dd>
          <dt>请求编号</dt><dd class="request-id"><span>{{ value(row.requestId) }}</span><el-tooltip v-if="row.requestId" content="复制请求编号"><el-button text :icon="Copy" aria-label="复制请求编号" @click="copyRequestId" /></el-tooltip></dd>
        </dl>
      </section>

      <el-collapse class="raw-collapse">
        <el-collapse-item name="raw">
          <template #title><span><CircleAlert :size="15" /> 原始脱敏 JSON</span></template>
          <pre>{{ raw }}</pre>
        </el-collapse-item>
      </el-collapse>
    </template>
  </el-drawer>
</template>

<style scoped>
.drawer-summary { display: flex; justify-content: space-between; align-items: start; gap: 12px; padding-bottom: 16px; border-bottom: 1px solid #e8edef; }
.drawer-eyebrow { color: #708085; font-size: 12px; }
.drawer-summary h3 { margin: 5px 0 0; color: #26383b; font-size: 16px; line-height: 1.5; }
.drawer-tags { display: flex; flex-wrap: wrap; justify-content: end; gap: 6px; }
.reason-alert { margin-top: 14px; }
.detail-section { margin-top: 22px; }
.detail-section h4 { display: flex; align-items: center; gap: 7px; margin: 0 0 10px; color: #3d5256; font-size: 13px; }
.detail-grid { display: grid; grid-template-columns: 88px minmax(0, 1fr); gap: 8px 12px; margin: 0; font-size: 13px; }
.detail-grid dt { color: #718084; }
.detail-grid dd { min-width: 0; margin: 0; color: #293b3f; overflow-wrap: anywhere; }
.request-id { display: flex; align-items: center; gap: 4px; }
.request-id > span { min-width: 0; overflow-wrap: anywhere; }
.raw-collapse { margin-top: 22px; border-top: 1px solid #e8edef; }
.raw-collapse :deep(.el-collapse-item__header span) { display: flex; align-items: center; gap: 7px; color: #596b6f; font-size: 13px; }
.raw-collapse pre { max-height: 300px; overflow: auto; margin: 0 0 12px; padding: 10px; border: 1px solid #e3e9ea; border-radius: 5px; background: #f7f9f9; color: #314347; font: 11px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
@media (max-width: 520px) { .drawer-summary { flex-direction: column; } .drawer-tags { justify-content: start; } .detail-grid { grid-template-columns: 72px minmax(0, 1fr); gap: 8px; } }
</style>
