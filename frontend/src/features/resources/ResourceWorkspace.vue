<script setup lang="ts">
// 资源是设备身份入口；软删除后仍可进入所属任务查询和下载历史日志。
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { Activity, Archive, ChartLine, Edit3, Eye, FileArchive, History, KeyRound, Network, Plus, Search, Server, Trash2 } from "lucide-vue-next";
import { api } from "../../shared/api";
import { confirmAction } from "../../shared/confirm";
import type { Resource, ResourceKind } from "../../shared/types";
import AsyncView from "../../shared/AsyncView.vue";
import { canManageOwnedRecord } from "../../shared/ownership";
import type { BulkOperationEntry } from "../../shared/bulkOperations";
import CreatorFilter from "../../shared/CreatorFilter.vue";
import ResourceBatchDelete from "./ResourceBatchDelete.vue";

const loadResourceEditor = () => import("./ResourceEditor.vue");
const loadCoredumpFiles = () => import("../coredumps/CoredumpFiles.vue");
const loadAuthenticationRecords = () => import("./AuthenticationRecordsDialog.vue");
const loadResourceMetrics = () => import("./ResourceMetricsDialog.vue");

const emit = defineEmits<{ tasks: [Resource]; createTask: [Resource] }>();
const props = defineProps<{ canWrite?: boolean; canCreate?: boolean; canCreateTask?: boolean; canControl?: boolean; userId?: string; isAdmin?: boolean }>();
const resources = ref<Resource[]>([]); const total = ref(0); const page = ref(1); const search = ref(""); const kind = ref<ResourceKind | undefined>();
const loading = ref(false); const editorOpen = ref(false); let generation = 0;
const coredumpOpen = ref(false), coredumpResource = ref<Resource>();
const authenticationRecordsOpen = ref(false), authenticationRecordsResource = ref<Resource>();
const metricsOpen = ref(false), metricsResource = ref<Resource>();
const includeDeleted = ref(false), selectedResource = ref<Resource>(), lastLoadedAt = ref("");
const showAll = ref(Boolean(props.isAdmin));
const createdBy = ref("");
const deleting = ref(new Set<string>());
const authenticating = ref(new Set<string>());
const selected = ref<Resource[]>([]);
const batchDeleting = ref(false);
const selectionGeneration = ref(0);
const table = ref<{ clearSelection: () => void; toggleRowSelection: (row: Resource, selected?: boolean) => void }>();
const narrowViewport = ref(false);
const narrowViewportQuery = window.matchMedia("(max-width: 767px)");
function syncViewport(event?: MediaQueryListEvent) {
  // 窄屏不固定操作列，避免固定列覆盖资源身份和网络地址。
  narrowViewport.value = event?.matches ?? narrowViewportQuery.matches;
}
const labels: Record<ResourceKind, string> = { HIKVISION_NETWORK: "海康网络设备", SERIAL_SERVER: "串口服务器" };
function kindLabel(value: ResourceKind) { return labels[value]; }
function kindIcon(value: ResourceKind) { return value === "HIKVISION_NETWORK" ? Network : Server; }
const networkCount = computed(() => resources.value.filter(resource => resource.kind === "HIKVISION_NETWORK").length);
const serialServerCount = computed(() => resources.value.filter(resource => resource.kind === "SERIAL_SERVER").length);
const deletedOnPage = computed(() => resources.value.filter(resource => resource.deletedAt).length);
async function load() {
  const current = ++generation; loading.value = true;
  try { const data = await api.resources(page.value, 20, {
    search: search.value.trim() || undefined,
    kind: kind.value,
    includeDeleted: includeDeleted.value ? "true" : undefined,
    createdBy: showAll.value ? createdBy.value || undefined : props.userId,
  }); if (current !== generation) return; resources.value = data.items; total.value = data.total; lastLoadedAt.value = new Date().toLocaleTimeString("zh-CN", { hour12: false }); }
  catch (error) { if (current === generation) ElMessage.error(error instanceof Error ? error.message : "读取资源失败"); }
  finally { if (current === generation) loading.value = false; }
}
function clearSelection(invalidate = false) {
  if (invalidate) selectionGeneration.value += 1;
  selected.value = []; table.value?.clearSelection();
}
function filter() { clearSelection(true); if (page.value === 1) void load(); else page.value = 1; }
function changeScope(value: boolean | string | number) {
  showAll.value = Boolean(value);
  createdBy.value = "";
  filter();
}
function formatDate(value?: string | null) {
  return value ? new Date(value).toLocaleString("zh-CN", { hour12: false, timeZone: "Asia/Shanghai" }) : "-";
}
function healthLabel(value?: Resource["healthStatus"]) {
  return ({ ONLINE: "当前连通", AUTH_FAILED: "HTTP 凭据失效", OFFLINE: "设备离线", ERROR: "认证检查异常" }[value ?? ""] ?? "尚未检查");
}
function healthTone(value?: Resource["healthStatus"]) {
  return ({ ONLINE: "success", AUTH_FAILED: "danger", OFFLINE: "warning", ERROR: "danger" }[value ?? ""] ?? "info");
}
function owns(resource?: Resource) { return Boolean(resource && canManageOwnedRecord(props.userId, props.isAdmin, resource.createdBy)); }
function permitted(resource?: Resource) { return Boolean(props.canWrite && owns(resource)); }
function canAuthenticate(resource?: Resource) {
  return Boolean(resource && resource.kind === "HIKVISION_NETWORK" && !resource.deletedAt && permitted(resource) && props.canControl);
}
function canBulkDelete(resource?: Resource) { return Boolean(!batchDeleting.value && resource && !resource.deletedAt && permitted(resource) && props.canControl); }
function updateSelection(items: Resource[]) { selected.value = items.filter(canBulkDelete); }
function canCreateTask(resource: Resource) { return Boolean(props.canCreateTask && !resource.deletedAt); }
function edit(resource?: Resource) { if (resource && !permitted(resource)) return; selectedResource.value = resource; editorOpen.value = true; }
function viewCoredumps(resource: Resource) { coredumpResource.value = resource; coredumpOpen.value = true; }
function viewAuthenticationRecords(resource: Resource) { authenticationRecordsResource.value = resource; authenticationRecordsOpen.value = true; }
/** 资源列表已由读取权限过滤，趋势入口不要求资源配置写权限。 */
function viewMetrics(resource: Resource) { metricsResource.value = resource; metricsOpen.value = true; }
async function remove(resource: Resource) {
  if (!permitted(resource)) return;
  if (batchDeleting.value || deleting.value.has(resource.id)) return;
  deleting.value = new Set(deleting.value).add(resource.id);
  try {
    const latest = await api.resource(resource.id);
    if (latest.deletedAt) { await load(); return; }
    const count = latest.taskCount ?? 0, unsettled = latest.unsettledTaskCount ?? 0;
    const message = count
      ? `资源“${latest.name}”关联 ${count} 个采集任务，其中 ${unsettled} 个尚未收束。删除后将停止关联采集，已有日志文件保留，可继续查询和下载。确认删除？`
      : `确认删除资源“${latest.name}”？资源将标记为已删除，已有日志文件保留。`;
    const ownerNotice = !props.isAdmin && count ? "关联任务若由其他用户创建，服务端将拒绝本次删除。" : "";
    if (!(await confirmAction(`${message}${ownerNotice}`, "确认删除设备资源"))) return;
    await api.deleteResource(latest.id, latest.version ?? 1);
    ElMessage.success("资源已标记删除，已有日志保留");
    await load();
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : "删除资源失败"); }
  finally { const next = new Set(deleting.value); next.delete(resource.id); deleting.value = next; }
}
async function authenticate(resource: Resource) {
  if (!canAuthenticate(resource) || authenticating.value.has(resource.id)) return;
  if (!(await confirmAction(`确认立即认证设备资源“${resource.name}”？将使用已保存的 HTTP 凭据检查设备并更新认证状态；失败可能停止关联采集任务。`, "确认设备认证"))) return;
  authenticating.value = new Set(authenticating.value).add(resource.id);
  try {
    await api.authenticateSavedResource(resource.id);
    ElMessage.success(`设备资源“${resource.name}”认证成功`);
    await load();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "设备认证失败");
    // 手动认证失败同样由服务端更新健康状态与关联任务，列表必须立即反映最终状态。
    await load();
  } finally {
    const next = new Set(authenticating.value); next.delete(resource.id); authenticating.value = next;
  }
}
async function completeBatch(entries: BulkOperationEntry[]) {
  const failed = new Set(entries.filter(entry => entry.status !== "success").map(entry => entry.id));
  await load();
  selected.value = resources.value.filter(resource => failed.has(resource.id) && canBulkDelete(resource));
  await nextTick();
  table.value?.clearSelection();
  selected.value.forEach(resource => table.value?.toggleRowSelection(resource, true));
}
watch(page, () => { clearSelection(true); void load(); });
watch(() => [props.userId, props.isAdmin], ([userId, isAdmin], previous) => {
  if (previous && (userId !== previous[0] || isAdmin !== previous[1])) {
    showAll.value = Boolean(isAdmin);
    createdBy.value = "";
    filter();
  }
});
onMounted(() => {
  syncViewport();
  narrowViewportQuery.addEventListener("change", syncViewport);
  void load();
});
onBeforeUnmount(() => {
  generation += 1;
  narrowViewportQuery.removeEventListener("change", syncViewport);
  clearSelection(true);
});
defineExpose({ reload: load });
</script>
<template>
  <section class="resource-summary" aria-label="设备资源概况">
    <div class="resource-summary-title"><span class="resource-summary-icon"><Server :size="20" /></span><div><h2>设备资源目录</h2><p>上次刷新 {{ lastLoadedAt || '--:--:--' }}</p></div></div>
    <div class="resource-stat"><span>资源总数</span><strong>{{ total.toLocaleString() }}</strong><small>当前筛选</small></div>
    <div class="resource-stat"><span>本页网络设备</span><strong class="resource-stat-active">{{ networkCount.toLocaleString() }}</strong><small>当前页资源</small></div>
    <div class="resource-stat"><span>本页串口服务器</span><strong>{{ serialServerCount.toLocaleString() }}</strong><small>{{ deletedOnPage }} 项已删除</small></div>
  </section>
  <div class="resource-toolbar" role="search">
    <el-input v-model="search" aria-label="搜索资源" placeholder="搜索名称或 IP" :prefix-icon="Search" clearable @keyup.enter="filter" />
    <el-select v-model="kind" aria-label="按资源类型筛选" clearable placeholder="全部资源" @change="filter"><el-option label="海康网络设备" value="HIKVISION_NETWORK" /><el-option label="串口服务器" value="SERIAL_SERVER" /></el-select>
    <el-checkbox :model-value="showAll" @change="changeScope">查看全部</el-checkbox>
    <CreatorFilter v-if="showAll" v-model="createdBy" @change="filter" />
    <el-checkbox v-model="includeDeleted" @change="filter">包含已删除资源</el-checkbox>
    <el-button @click="filter">筛选</el-button><ResourceBatchDelete :items="selected" :disabled="batchDeleting" :selection-generation="selectionGeneration" @processing="batchDeleting = $event" @completed="completeBatch" /><el-button v-if="props.canCreate" type="primary" :icon="Plus" @click="edit()">新建资源</el-button>
  </div>
  <el-table ref="table" row-key="id" reserve-selection :data="resources" v-loading="loading" scrollbar-always-on class="data-table resource-table" empty-text="暂无设备资源" @selection-change="updateSelection">
    <el-table-column type="selection" width="48" :selectable="canBulkDelete" />
    <el-table-column label="资源身份" min-width="250"><template #default="{ row }"><div class="resource-identity"><span class="resource-kind-icon" :class="row.kind === 'HIKVISION_NETWORK' ? 'network' : 'serial'"><component :is="kindIcon(row.kind)" :size="17" /></span><div><strong>{{ row.name }}</strong><div class="resource-type-line"><span>{{ kindLabel(row.kind) }}</span><el-tag v-if="row.deletedAt" type="info" size="small">已删除 · 日志保留</el-tag></div></div></div></template></el-table-column>
    <el-table-column label="网络地址" min-width="165"><template #default="{ row }"><span class="resource-ip">{{ row.ip }}</span><small class="resource-subtle">{{ row.kind === 'HIKVISION_NETWORK' ? 'HTTP 设备入口' : 'Telnet 串口入口' }}</small></template></el-table-column>
    <el-table-column label="设备身份" min-width="270"><template #default="{ row }"><div class="resource-device-meta"><strong>{{ row.model || (row.kind === 'SERIAL_SERVER' ? '串口服务器' : '-') }}</strong><span>序列号 {{ row.subSerialNumber || '-' }}</span><span>软件 {{ row.softwareVersion || '-' }}</span></div></template></el-table-column>
    <el-table-column label="设备认证" min-width="175"><template #default="{ row }"><template v-if="row.kind === 'HIKVISION_NETWORK'"><el-tag :type="healthTone(row.healthStatus)" size="small">{{ healthLabel(row.healthStatus) }}</el-tag><small class="resource-subtle">检查 {{ formatDate(row.healthCheckedAt) }}</small></template><span v-else>-</span></template></el-table-column>
    <el-table-column label="任务" width="120" align="right"><template #default="{ row }"><div class="resource-task-count"><strong>{{ row.taskCount ?? 0 }}</strong><span><Activity :size="13" />{{ row.activeTaskCount ?? 0 }} 采集中</span></div></template></el-table-column>
    <el-table-column label="创建用户" min-width="130"><template #default="{ row }">{{ row.createdByName || row.createdBy || "未知" }}</template></el-table-column>
    <el-table-column label="创建时间（北京时间）" min-width="180"><template #default="{ row }">{{ formatDate(row.createdAt) }}</template></el-table-column>
    <el-table-column label="删除时间（北京时间）" min-width="180"><template #default="{ row }">{{ formatDate(row.deletedAt) }}</template></el-table-column>
    <el-table-column label="操作" width="364" :fixed="narrowViewport ? false : 'right'"><template #default="{ row }"><div class="resource-actions">
      <el-button text type="primary" :icon="row.deletedAt ? Archive : Eye" @click="emit('tasks', row)">{{ row.deletedAt ? '查看历史日志' : '查看任务' }}</el-button>
      <el-tooltip v-if="row.kind === 'HIKVISION_NETWORK'" content="查看认证记录"><el-button text :icon="History" aria-label="查看认证记录" @click="viewAuthenticationRecords(row)" /></el-tooltip>
      <el-tooltip v-if="canAuthenticate(row)" content="立即认证设备"><el-button text :icon="KeyRound" aria-label="立即认证设备" :loading="authenticating.has(row.id)" :disabled="authenticating.has(row.id)" @click="authenticate(row)" /></el-tooltip>
      <el-tooltip v-if="row.kind === 'HIKVISION_NETWORK'" content="查看 coredump 文件"><el-button text :icon="FileArchive" aria-label="查看 coredump 文件" @click="viewCoredumps(row)" /></el-tooltip>
      <el-tooltip v-if="row.kind === 'HIKVISION_NETWORK' && row.enableResourceMonitor && !row.deletedAt" content="查看 CPU 与内存趋势"><el-button text :icon="ChartLine" aria-label="查看 CPU 与内存趋势" @click="viewMetrics(row)" /></el-tooltip>
      <el-tooltip v-if="canCreateTask(row)" content="新建采集任务"><el-button text type="primary" :icon="Plus" aria-label="新建采集任务" @click="emit('createTask', row)" /></el-tooltip>
      <el-tooltip v-if="!row.deletedAt && permitted(row)" content="编辑资源"><el-button text :icon="Edit3" aria-label="编辑资源" @click="edit(row)" /></el-tooltip>
      <el-tooltip v-if="!row.deletedAt && permitted(row) && props.canControl" content="删除资源"><el-button text type="danger" :icon="Trash2" aria-label="删除资源" :disabled="batchDeleting || deleting.has(row.id)" @click="remove(row)" /></el-tooltip>
    </div></template></el-table-column>
  </el-table>
  <el-pagination v-model:current-page="page" :page-size="20" :total="total" layout="total, prev, pager, next" />
  <AsyncView
    v-if="editorOpen"
    overlay
    :loader="loadResourceEditor"
    :component-props="{ modelValue: editorOpen, resource: selectedResource, canEdit: !selectedResource || permitted(selectedResource) }"
    :listeners="{ 'update:modelValue': (value: boolean) => editorOpen = value, saved: () => load() }"
  />
  <AsyncView v-if="coredumpOpen" overlay :loader="loadCoredumpFiles"
    :component-props="{ modelValue: coredumpOpen, resource: coredumpResource }"
    :listeners="{ 'update:modelValue': (value: boolean) => coredumpOpen = value }" />
  <AsyncView v-if="authenticationRecordsOpen" overlay :loader="loadAuthenticationRecords"
    :component-props="{ modelValue: authenticationRecordsOpen, resource: authenticationRecordsResource }"
    :listeners="{ 'update:modelValue': (value: boolean) => authenticationRecordsOpen = value }" />
  <AsyncView v-if="metricsOpen" overlay :loader="loadResourceMetrics"
    :component-props="{ modelValue: metricsOpen, resource: metricsResource }"
    :listeners="{ 'update:modelValue': (value: boolean) => metricsOpen = value }" />
</template>
<style scoped>
.resource-actions { display: flex; align-items: center; gap: 2px; white-space: nowrap; }
.resource-actions :deep(.el-button + .el-button) { margin-left: 0; }
</style>
