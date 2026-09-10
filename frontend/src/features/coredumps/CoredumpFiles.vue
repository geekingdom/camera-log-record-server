<script setup lang="ts">
// 资源级 coredump 文件页：冻结前不下载，批量导出始终等待服务端作业完成。
import { onBeforeUnmount, ref, watch } from "vue";
import { Download, RefreshCw, X } from "lucide-vue-next";
import { ElMessage } from "element-plus";
import { api } from "../../shared/api";
import { confirmAction } from "../../shared/confirm";
import type { CoredumpFile, Resource } from "../../shared/types";
import { coredumpFileStatus } from "./coredumpStatus";

const open = defineModel<boolean>({ required: true });
const props = defineProps<{ resource?: Resource }>();
const files = ref<CoredumpFile[]>([]);
const selected = ref<string[]>([]);
const selectionGeneration = ref(0);
const page = ref(1), total = ref(0), loading = ref(false), exporting = ref(false);
const name = ref(""), range = ref<[Date, Date]>();
const exportStatus = ref(""), exportProgress = ref("");
let lifecycleGeneration = 0;
let listGeneration = 0;
let activeExport: string | undefined;
let refreshTimer: ReturnType<typeof setInterval> | undefined;
let listPending = false;

const statusLabels: Record<string, string> = {
  RECEIVING: "正在接收", FREEZING: "正在冻结", FROZEN: "已冻结",
  RETIRING: "等待读取结束", DELETING: "正在清理副本",
  QUEUED: "等待导出", RUNNING: "正在导出", SUCCEEDED: "导出完成",
  FAILED: "导出失败", CANCELLED: "已取消",
};
function statusLabel(status?: string) { return statusLabels[status ?? ""] ?? status ?? "未知"; }
function statusTone(status?: string) {
  if (status === "FROZEN" || status === "SUCCEEDED") return "success";
  if (status === "FAILED" || status === "CANCELLED") return "danger";
  return "warning";
}
function time(value?: string) {
  if (!value) return "-";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
}
function bytes(value?: number) { return Number.isFinite(value) ? value!.toLocaleString("zh-CN") : "-"; }
function navigateDownload(url: string, filename: string) {
  const link = document.createElement("a");
  link.href = url; link.download = filename; link.click();
}
function fail(error: unknown) { ElMessage.error(error instanceof Error ? error.message : "操作失败"); }
function currentLifecycle(generation: number, resourceId: string) {
  return generation === lifecycleGeneration && open.value && props.resource?.id === resourceId;
}
async function load(quiet = false) {
  const resourceId = props.resource?.id;
  if (!resourceId || (quiet && listPending)) return;
  const current = ++listGeneration, lifecycle = lifecycleGeneration;
  listPending = true;
  if (!quiet) { loading.value = true; selected.value = []; selectionGeneration.value++; }
  try {
    const result = await api.coredumps(resourceId, page.value, 50, {
      name: name.value.trim() || undefined,
      receivedFrom: range.value?.[0].toISOString(), receivedTo: range.value?.[1].toISOString(),
    });
    if (current === listGeneration && currentLifecycle(lifecycle, resourceId)) {
      files.value = result.items; total.value = result.total;
    }
  } catch (error) { if (!quiet && current === listGeneration && currentLifecycle(lifecycle, resourceId)) fail(error); }
  finally { if (current === listGeneration) { loading.value = false; listPending = false; } }
}
function filter() { if (page.value === 1) void load(); else page.value = 1; }
async function download(file: CoredumpFile) {
  if (file.status !== "FROZEN") return;
  const lifecycle = lifecycleGeneration, resourceId = props.resource?.id;
  if (!resourceId) return;
  try {
    const ticket = await api.coredumpBrowserDownload(file.id);
    if (!currentLifecycle(lifecycle, resourceId)) return;
    navigateDownload(ticket.url, file.name);
  }
  catch (error) { if (currentLifecycle(lifecycle, resourceId)) fail(error); }
}
async function pollExport(id: string, lifecycle: number, resourceId: string) {
  for (;;) {
    if (!currentLifecycle(lifecycle, resourceId)) throw new Error("页面已关闭");
    const job = await api.coredumpExport(id);
    if (!currentLifecycle(lifecycle, resourceId)) throw new Error("页面已关闭");
    exportStatus.value = statusLabel(job.status);
    if (["SUCCEEDED", "CANCELLED"].includes(job.status)) return job;
    if (["FAILED", "EXPIRED"].includes(job.status)) throw new Error(job.error || `导出状态：${statusLabel(job.status)}`);
    await new Promise(resolve => window.setTimeout(resolve, 1000));
  }
}
async function exportFiles() {
  if (exporting.value) return;
  if (!selected.value.length) return ElMessage.warning("请选择至少一个 coredump 文件");
  const fileIds = [...selected.value], lifecycle = lifecycleGeneration, resourceId = props.resource?.id;
  if (!resourceId || !(await confirmAction(`确认导出所选的 ${fileIds.length} 个 coredump 文件吗？`, "确认导出 coredump"))) return;
  if (!currentLifecycle(lifecycle, resourceId)) return;
  exporting.value = true; exportStatus.value = "等待导出"; exportProgress.value = "";
  try {
    const job = await api.createCoredumpExport(fileIds);
    if (!currentLifecycle(lifecycle, resourceId)) return;
    activeExport = job.id;
    const finished = await pollExport(job.id, lifecycle, resourceId);
    if (!currentLifecycle(lifecycle, resourceId) || finished.status !== "SUCCEEDED") return;
    const filename = finished.filename || `${job.id}.zip`;
    const ticket = await api.coredumpBrowserDownload(job.id, true);
    if (!currentLifecycle(lifecycle, resourceId)) return;
    navigateDownload(ticket.url, filename);
    exportProgress.value = finished.bytes === undefined ? "" : `${finished.bytes.toLocaleString("zh-CN")} 字节`;
  } catch (error) { if (currentLifecycle(lifecycle, resourceId)) fail(error); }
  finally { if (currentLifecycle(lifecycle, resourceId)) { exporting.value = false; activeExport = undefined; } }
}
async function cancelExport() {
  const identifier = activeExport, lifecycle = lifecycleGeneration, resourceId = props.resource?.id;
  if (!identifier || !resourceId) return;
  if (!(await confirmAction("确认取消当前 coredump 导出吗？", "确认取消导出"))) return;
  if (!currentLifecycle(lifecycle, resourceId) || activeExport !== identifier) return;
  try { await api.cancelCoredumpExport(identifier); }
  catch (error) { if (currentLifecycle(lifecycle, resourceId) && activeExport === identifier) fail(error); }
}
watch(page, () => void load());
watch(() => [open.value, props.resource?.id] as const, ([visible]) => {
  lifecycleGeneration++;
  clearInterval(refreshTimer);
  ++listGeneration; listPending = false; loading.value = false;
  files.value = []; selected.value = []; total.value = 0; exportStatus.value = exportProgress.value = "";
  activeExport = undefined; exporting.value = false;
  if (visible) {
    page.value = 1; void load();
    // 仅在文件窗口打开时轮询；后台刷新保留所选文件，不允许旧资源响应回写。
    refreshTimer = setInterval(() => void load(true), 5000);
  }
}, { immediate: true });
onBeforeUnmount(() => { clearInterval(refreshTimer); lifecycleGeneration++; activeExport = undefined; });
</script>

<template>
  <el-drawer v-model="open" :title="`${props.resource?.name ?? '设备资源'} · Coredump 文件`" size="min(1180px, 96vw)" destroy-on-close>
    <div class="coredump-toolbar">
      <el-input v-model="name" aria-label="按文件名筛选 coredump" placeholder="按文件名筛选" clearable @keyup.enter="filter" />
      <el-date-picker v-model="range" type="datetimerange" range-separator="至" start-placeholder="首次发现起始时间" end-placeholder="首次发现截止时间" />
      <el-button @click="filter">筛选</el-button>
      <el-tooltip content="刷新 coredump 文件"><el-button :icon="RefreshCw" aria-label="刷新 coredump 文件" @click="load()" /></el-tooltip>
      <el-button type="primary" :icon="Download" :loading="exporting" @click="exportFiles">导出所选</el-button>
      <el-button v-if="activeExport" text type="danger" :icon="X" @click="cancelExport">取消导出</el-button>
    </div>
    <p v-if="exportStatus" class="coredump-export-status">{{ exportStatus }}<template v-if="exportProgress"> · {{ exportProgress }}</template></p>
    <el-table :key="selectionGeneration" :data="files" v-loading="loading" row-key="id" class="data-table coredump-table" @selection-change="(items: CoredumpFile[]) => selected = items.map(item => item.id)">
      <el-table-column type="selection" width="48" :reserve-selection="true" />
      <el-table-column label="文件名" min-width="260"><template #default="{ row }"><strong class="coredump-name">{{ row.name }}</strong><small>{{ row.nodeId }}</small></template></el-table-column>
      <el-table-column label="首次发现时间（北京时间）" min-width="210"><template #header><el-tooltip content="服务器首次扫描到文件的时间，不是传输完成时间。"><span>首次发现时间（北京时间）</span></el-tooltip></template><template #default="{ row }">{{ time(row.firstSeenAt || row.receivedAt) }}</template></el-table-column>
      <el-table-column label="文件修改时间（北京时间）" min-width="210"><template #header><el-tooltip content="NFS 文件的最后修改时间。传输过程中会更新，也可能由设备设置，因此可能晚于首次发现时间。"><span>文件修改时间（北京时间）</span></el-tooltip></template><template #default="{ row }">{{ time(row.sourceModifiedAt) }}</template></el-table-column>
      <el-table-column label="大小" width="130" align="right"><template #default="{ row }">{{ bytes(row.size) }} 字节</template></el-table-column>
      <el-table-column label="状态" width="145"><template #default="{ row }"><el-tooltip content="文件已稳定表示连续扫描至少10秒未发现变化，不是设备发送的完成确认；导出时仍会校验并创建固定副本。"><el-tag :type="row.sourceState === 'STABLE' && row.status === 'RECEIVING' ? 'success' : statusTone(row.status)">{{ coredumpFileStatus(row) }}</el-tag></el-tooltip></template></el-table-column>
      <el-table-column label="操作" width="92" fixed="right"><template #default="{ row }"><el-tooltip :disabled="row.status === 'FROZEN'" :content="row.status === 'FROZEN' ? '' : '文件冻结后才能下载'"><el-button text :icon="Download" aria-label="下载 coredump 文件" :disabled="row.status !== 'FROZEN'" @click="download(row)" /></el-tooltip></template></el-table-column>
    </el-table>
    <el-pagination v-model:current-page="page" :page-size="50" :total="total" layout="total, prev, pager, next" />
    <template #footer><el-button @click="open = false">关闭</el-button></template>
  </el-drawer>
</template>

<style scoped>
.coredump-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 14px; }
.coredump-toolbar .el-input { width: min(280px, 100%); }
.coredump-export-status { margin: 0 0 12px; color: #59676a; font-size: 12px; }
.coredump-name { display: block; color: #293d40; overflow-wrap: anywhere; }
.coredump-name + small { display: block; margin-top: 5px; color: #819095; font: 10px ui-monospace, monospace; overflow-wrap: anywhere; }
.coredump-table :deep(.el-table__cell) { padding-top: 14px; padding-bottom: 14px; }
@media (max-width: 620px) { .coredump-toolbar .el-input, .coredump-toolbar .el-date-editor { width: 100%; } }
</style>
