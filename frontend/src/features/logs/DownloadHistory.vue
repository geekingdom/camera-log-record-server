<script setup lang="ts">
// 下载历史只读取当前任务的已提交作业；服务端决定制品能否复用，页面不会重新创建作业。
import { onBeforeUnmount, ref, watch } from "vue";
import { Copy, Download, RefreshCw } from "lucide-vue-next";
import { ElMessage } from "element-plus";
import { api } from "../../shared/api";
import type { DownloadHistoryItem } from "../../shared/types";
import { formatDashboardBytes } from "../dashboard/dashboard";

const props = defineProps<{ taskId: string }>();
const pageSize = 20;
const page = ref(1);
const total = ref(0);
const records = ref<DownloadHistoryItem[]>([]);
const loading = ref(false);
const downloading = ref("");
let workspaceGeneration = 0;
let requestGeneration = 0;

const labels: Record<string, string> = {
  QUEUED: "等待执行", RUNNING: "正在生成", SUCCEEDED: "已完成", FAILED: "执行失败", CANCELLED: "已取消", EXPIRED: "已过期",
};

function statusLabel(record: DownloadHistoryItem) {
  return record.status === "SUCCEEDED" && !record.downloadAvailable ? "已过期" : labels[record.status] ?? record.status;
}
function statusTone(record: DownloadHistoryItem) {
  if (record.status === "SUCCEEDED" && record.downloadAvailable) return "success";
  if (record.status === "SUCCEEDED" || record.status === "RUNNING" || record.status === "QUEUED") return "info";
  return record.status === "EXPIRED" ? "warning" : "danger";
}
function dateTime(value?: string) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
}
function bytes(value?: number) { return formatDashboardBytes(value); }
function failure(record: DownloadHistoryItem) { return record.error || "-"; }
async function copyLink(record: DownloadHistoryItem) {
  const url = new URL(api.downloadContentUrl(record.id), location.origin).href;
  try { await navigator.clipboard.writeText(url); ElMessage.success("下载链接已复制"); }
  catch {
    const input = document.createElement("textarea"); input.value = url; document.body.append(input); input.select();
    const copied = document.execCommand("copy"); input.remove(); if (copied) ElMessage.success("下载链接已复制"); else ElMessage.error("复制下载链接失败");
  }
}

// 翻页、刷新与切换任务均推进请求代次，迟到响应不能覆盖当前任务的列表。
async function load() {
  const workspace = workspaceGeneration;
  const current = ++requestGeneration;
  loading.value = true;
  try {
    const result = await api.taskDownloads(props.taskId, page.value);
    if (workspace === workspaceGeneration && current === requestGeneration) {
      records.value = result.items;
      total.value = result.total;
    }
  } catch (error) {
    if (workspace === workspaceGeneration && current === requestGeneration) {
      records.value = [];
      total.value = 0;
      ElMessage.error(error instanceof Error ? error.message : "读取下载记录失败");
    }
  } finally {
    if (workspace === workspaceGeneration && current === requestGeneration) loading.value = false;
  }
}
function changePage(next: number) {
  page.value = next;
  void load();
}
async function download(record: DownloadHistoryItem) {
  if (!record.downloadAvailable || downloading.value) return;
  downloading.value = record.id;
  try {
    const link = document.createElement("a");
    link.href = api.downloadContentUrl(record.id);
    link.download = record.filename || `${record.id}.tar.gz`;
    link.click();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "下载制品失败");
    void load();
  } finally {
    downloading.value = "";
  }
}
watch(() => props.taskId, () => {
  workspaceGeneration++;
  requestGeneration++;
  page.value = 1;
  records.value = [];
  total.value = 0;
  downloading.value = "";
  void load();
}, { immediate: true });
onBeforeUnmount(() => { workspaceGeneration++; requestGeneration++; });
</script>

<template>
  <section class="download-history" aria-label="下载记录">
    <div class="section-heading">
      <div><h2>下载记录</h2><p>成功且未过期的制品可直接重新下载，不会重新创建作业。</p></div>
      <el-tooltip content="刷新下载记录"><el-button :icon="RefreshCw" :loading="loading" aria-label="刷新下载记录" @click="load" /></el-tooltip>
    </div>
    <div class="download-history-table">
      <el-table v-loading="loading" :data="records" size="small" empty-text="当前任务没有下载记录">
        <el-table-column label="创建时间（北京时间）" min-width="180"><template #default="{ row }">{{ dateTime(row.createdAt) }}</template></el-table-column>
        <el-table-column label="状态" min-width="110"><template #default="{ row }"><span class="download-history-status" :class="`is-${statusTone(row)}`">{{ statusLabel(row) }}</span></template></el-table-column>
        <el-table-column label="进度" min-width="90"><template #default="{ row }">{{ row.progress == null ? "-" : `${Math.min(100, Math.max(0, row.progress))}%` }}</template></el-table-column>
        <el-table-column label="制品大小" min-width="105"><template #default="{ row }">{{ bytes(row.bytes) }}</template></el-table-column>
        <el-table-column label="到期时间（北京时间）" min-width="180"><template #default="{ row }">{{ dateTime(row.expiresAt) }}</template></el-table-column>
        <el-table-column label="说明" min-width="150"><template #default="{ row }">{{ row.downloadAvailable ? "制品可重新下载" : row.status === "SUCCEEDED" ? "制品已过期或已清理" : failure(row) }}</template></el-table-column>
        <el-table-column label="操作" width="118" fixed="right"><template #default="{ row }"><el-tooltip :content="row.downloadAvailable ? '重新下载既有制品' : row.status === 'SUCCEEDED' ? '制品已过期，无法下载' : '作业尚未生成可下载制品'"><el-button text :icon="Download" :loading="downloading === row.id" :disabled="!row.downloadAvailable || Boolean(downloading)" :aria-label="`重新下载 ${row.id}`" @click="download(row)" /></el-tooltip><el-tooltip content="复制下载链接"><el-button text :icon="Copy" :disabled="!row.downloadAvailable" :aria-label="`复制下载链接 ${row.id}`" @click="copyLink(row)" /></el-tooltip></template></el-table-column>
      </el-table>
    </div>
    <div class="download-history-footer"><span>{{ total }} 条下载记录</span><el-pagination v-if="total > pageSize" background layout="prev, pager, next" :current-page="page" :page-size="pageSize" :total="total" @current-change="changePage" /></div>
  </section>
</template>

<style scoped>
.download-history { min-width: 0; padding-top: 20px; }
.section-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
.section-heading h2 { margin: 0; color: #304346; font-size: 16px; }.section-heading p { margin: 5px 0 0; color: #768589; font-size: 13px; line-height: 1.55; }
.download-history-table { max-width: 100%; overflow-x: auto; border: 1px solid #dfe8e7; border-radius: 6px; }.download-history-table :deep(.el-table) { min-width: 920px; }
.download-history-status { display: inline-flex; align-items: center; min-height: 22px; padding: 0 7px; border: 1px solid #b9c7c5; border-radius: 3px; color: #516264; background: #f6f8f8; font-size: 12px; line-height: 1; white-space: nowrap; }.download-history-status.is-success { border-color: #9bcfbd; color: #087568; background: #edf8f3; }.download-history-status.is-warning { border-color: #e6c580; color: #8a5a00; background: #fff8e8; }.download-history-status.is-danger { border-color: #e4b8b9; color: #a33f43; background: #fff1f1; }
.download-history-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 16px; color: #748387; font-size: 13px; }
@media (max-width: 600px) { .download-history { padding-top: 16px; }.section-heading { gap: 10px; }.section-heading p { max-width: 250px; }.download-history-footer { align-items: flex-start; flex-direction: column; }.download-history-footer :deep(.el-pagination) { max-width: 100%; overflow-x: auto; } }
</style>
