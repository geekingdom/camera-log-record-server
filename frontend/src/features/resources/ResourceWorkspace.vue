<script setup lang="ts">
// 资源是设备身份入口；软删除后仍可进入所属任务查询和下载历史日志。
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { Activity, Archive, Edit3, Eye, Network, Plus, Search, Server, Trash2 } from "lucide-vue-next";
import { api } from "../../shared/api";
import { confirmAction } from "../../shared/confirm";
import type { Resource, ResourceKind } from "../../shared/types";
import ResourceEditor from "./ResourceEditor.vue";

const emit = defineEmits<{ tasks: [Resource] }>();
const resources = ref<Resource[]>([]); const total = ref(0); const page = ref(1); const search = ref(""); const kind = ref<ResourceKind | undefined>();
const loading = ref(false); const editorOpen = ref(false); let generation = 0;
const includeDeleted = ref(false), selectedResource = ref<Resource>(), lastLoadedAt = ref("");
const deleting = ref(new Set<string>());
const labels: Record<ResourceKind, string> = { HIKVISION_NETWORK: "海康网络设备", SERIAL_SERVER: "串口服务器" };
function kindLabel(value: ResourceKind) { return labels[value]; }
function kindIcon(value: ResourceKind) { return value === "HIKVISION_NETWORK" ? Network : Server; }
const networkCount = computed(() => resources.value.filter(resource => resource.kind === "HIKVISION_NETWORK").length);
const serialServerCount = computed(() => resources.value.filter(resource => resource.kind === "SERIAL_SERVER").length);
const deletedOnPage = computed(() => resources.value.filter(resource => resource.deletedAt).length);
async function load() {
  const current = ++generation; loading.value = true;
  try { const data = await api.resources(page.value, 20, { search: search.value.trim() || undefined, kind: kind.value,
    includeDeleted: includeDeleted.value ? "true" : undefined }); if (current !== generation) return; resources.value = data.items; total.value = data.total; lastLoadedAt.value = new Date().toLocaleTimeString("zh-CN", { hour12: false }); }
  catch (error) { if (current === generation) ElMessage.error(error instanceof Error ? error.message : "读取资源失败"); }
  finally { if (current === generation) loading.value = false; }
}
function filter() { if (page.value === 1) void load(); else page.value = 1; }
function edit(resource?: Resource) { selectedResource.value = resource; editorOpen.value = true; }
async function remove(resource: Resource) {
  if (deleting.value.has(resource.id)) return;
  deleting.value = new Set(deleting.value).add(resource.id);
  try {
    const latest = await api.resource(resource.id);
    if (latest.deletedAt) { await load(); return; }
    const count = latest.taskCount ?? 0, active = latest.activeTaskCount ?? 0;
    const message = count
      ? `资源“${latest.name}”关联 ${count} 个采集任务，其中 ${active} 个尚未停止。删除后将停止关联采集，已有日志文件保留，可继续查询和下载。确认删除？`
      : `确认删除资源“${latest.name}”？资源将标记为已删除，已有日志文件保留。`;
    if (!(await confirmAction(message, "确认删除设备资源"))) return;
    await api.deleteResource(latest.id, latest.version ?? 1);
    ElMessage.success("资源已标记删除，已有日志保留");
    await load();
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : "删除资源失败"); }
  finally { const next = new Set(deleting.value); next.delete(resource.id); deleting.value = next; }
}
watch(page, () => void load()); onMounted(load);
onBeforeUnmount(() => ++generation);
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
    <el-checkbox v-model="includeDeleted" @change="filter">包含已删除资源</el-checkbox>
    <el-button @click="filter">筛选</el-button><el-button type="primary" :icon="Plus" @click="edit()">新建资源</el-button>
  </div>
  <el-table :data="resources" v-loading="loading" scrollbar-always-on class="data-table resource-table" empty-text="暂无设备资源">
    <el-table-column label="资源身份" min-width="250"><template #default="{ row }"><div class="resource-identity"><span class="resource-kind-icon" :class="row.kind === 'HIKVISION_NETWORK' ? 'network' : 'serial'"><component :is="kindIcon(row.kind)" :size="17" /></span><div><strong>{{ row.name }}</strong><div class="resource-type-line"><span>{{ kindLabel(row.kind) }}</span><el-tag v-if="row.deletedAt" type="info" size="small">已删除 · 日志保留</el-tag></div></div></div></template></el-table-column>
    <el-table-column label="网络地址" min-width="165"><template #default="{ row }"><span class="resource-ip">{{ row.ip }}</span><small class="resource-subtle">{{ row.kind === 'HIKVISION_NETWORK' ? 'HTTP 设备入口' : 'Telnet 串口入口' }}</small></template></el-table-column>
    <el-table-column label="设备身份" min-width="270"><template #default="{ row }"><div class="resource-device-meta"><strong>{{ row.model || (row.kind === 'SERIAL_SERVER' ? '串口服务器' : '-') }}</strong><span>序列号 {{ row.subSerialNumber || '-' }}</span><span>软件 {{ row.softwareVersion || '-' }}</span></div></template></el-table-column>
    <el-table-column label="任务" width="120" align="right"><template #default="{ row }"><div class="resource-task-count"><strong>{{ row.taskCount ?? 0 }}</strong><span><Activity :size="13" />{{ row.activeTaskCount ?? 0 }} 活跃</span></div></template></el-table-column>
    <el-table-column label="操作" width="220" fixed="right"><template #default="{ row }"><div class="resource-actions">
      <el-button text type="primary" :icon="row.deletedAt ? Archive : Eye" @click="emit('tasks', row)">{{ row.deletedAt ? '查看历史日志' : '查看任务' }}</el-button>
      <el-tooltip v-if="!row.deletedAt" content="编辑资源"><el-button text :icon="Edit3" aria-label="编辑资源" @click="edit(row)" /></el-tooltip>
      <el-tooltip v-if="!row.deletedAt" content="删除资源"><el-button text type="danger" :icon="Trash2" aria-label="删除资源" :disabled="deleting.has(row.id)" @click="remove(row)" /></el-tooltip>
    </div></template></el-table-column>
  </el-table>
  <el-pagination v-model:current-page="page" :page-size="20" :total="total" layout="total, prev, pager, next" />
  <ResourceEditor v-model="editorOpen" :resource="selectedResource" @saved="load" />
</template>
<style scoped>
.resource-actions { display: flex; align-items: center; gap: 2px; white-space: nowrap; }
.resource-actions :deep(.el-button + .el-button) { margin-left: 0; }
</style>
