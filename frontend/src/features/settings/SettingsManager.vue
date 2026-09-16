<script setup lang="ts">
// 后台设置页只管理持久化配置；节点在线状态始终来自后端合并的 worker 心跳。
import { onMounted, ref } from "vue";
import { Database, Edit3, Gauge, Plus, RefreshCw, Save, ServerCog, Settings2, SlidersHorizontal, Trash2 } from "lucide-vue-next";
import { ElMessage } from "element-plus";
import { ApiError } from "../../shared/api";
import { confirmAction } from "../../shared/confirm";
import { settingsApi, type NodeConfig, type NodeRegistration, type PlatformSettings } from "./api";
import { DEFAULT_WRITE_LATENCY_LIMIT_MS, MAX_CLUSTER_CAPACITY, MAX_NODE_CAPACITY, MAX_WRITE_LATENCY_LIMIT_MS, MIN_WRITE_LATENCY_LIMIT_MS, normalizeCapacity } from "./settingsForm";
import { formatWriteLatencyLimit } from "../../shared/nodeWriteLatency";
import ResourceMonitorSettings from "./ResourceMonitorSettings.vue";
import RecordRetentionSettings from "./RecordRetentionSettings.vue";

const loading = ref(false);
const savingRetention = ref(false);
const savingNode = ref(false);
const deletingNode = ref<string>();
const forbidden = ref(false);
const settings = ref<PlatformSettings>();
const activeModule = ref("platform");
const retentionDays = ref(7);
const clusterCapacity = ref(500);
const liveLogBufferMiB = ref(10);
const nodes = ref<NodeConfig[]>([]);
const nodeDialog = ref(false);
const editingNode = ref<NodeConfig>();
const nodeForm = ref<NodeRegistration>({ id: "", url: "", capacity: 10, accepting: true });
const nodeNetworksText = ref("");

async function load() {
  loading.value = true;
  forbidden.value = false;
  try {
    const [platform, nodeList] = await Promise.all([settingsApi.platform(), settingsApi.nodes()]);
    settings.value = platform;
    retentionDays.value = platform.retentionDays;
    clusterCapacity.value = normalizeCapacity(platform.clusterCapacity ?? 500, MAX_CLUSTER_CAPACITY);
    liveLogBufferMiB.value = platform.liveLogBufferMiB ?? 10;
    nodes.value = nodeList.items;
  } catch (error) {
    if (error instanceof ApiError && error.status === 403) forbidden.value = true;
    else ElMessage.error(error instanceof Error ? error.message : "读取后台配置失败");
  } finally {
    loading.value = false;
  }
}

async function savePlatformSettings() {
  if (savingRetention.value || !settings.value) return;
  if (!await confirmAction(`确认保存日志保留 ${retentionDays.value} 天、集群总并发上限 ${clusterCapacity.value} 个任务、实时日志内存上限 ${liveLogBufferMiB.value} MiB 吗？`, "确认保存配置")) return;
  savingRetention.value = true;
  try {
    settings.value = await settingsApi.updatePlatform({ retentionDays: retentionDays.value, clusterCapacity: clusterCapacity.value, liveLogBufferMiB: liveLogBufferMiB.value, version: settings.value.version });
    retentionDays.value = settings.value.retentionDays;
    clusterCapacity.value = normalizeCapacity(settings.value.clusterCapacity ?? clusterCapacity.value, MAX_CLUSTER_CAPACITY);
    liveLogBufferMiB.value = settings.value.liveLogBufferMiB;
    ElMessage.success("平台配置已保存");
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      ElMessage.warning("配置已被其他管理员修改，已刷新最新值");
      await load();
    } else ElMessage.error(error instanceof Error ? error.message : "保存日志保留期失败");
  } finally {
    savingRetention.value = false;
  }
}

function openRegister(node?: NodeConfig) {
  editingNode.value = undefined;
  nodeForm.value = node
    ? { id: node.id, url: node.reportedUrl ?? node.url, capacity: normalizeCapacity(node.capacity, MAX_NODE_CAPACITY), accepting: node.accepting, inputRateLimitMiB: node.inputRateLimitMiB ?? 0, writeLatencyLimitMs: node.writeLatencyLimitMs ?? DEFAULT_WRITE_LATENCY_LIMIT_MS, isGeneralNode: node.isGeneralNode ?? true }
    : { id: "", url: "", capacity: 10, accepting: true, inputRateLimitMiB: 50, writeLatencyLimitMs: DEFAULT_WRITE_LATENCY_LIMIT_MS, isGeneralNode: true };
  nodeNetworksText.value = (node?.resourceNetworks ?? []).join("\n");
  nodeDialog.value = true;
}

function openEdit(node: NodeConfig) {
  editingNode.value = node;
  nodeForm.value = { id: node.id, url: node.url, capacity: node.capacity, accepting: node.accepting, writeLatencyLimitMs: node.writeLatencyLimitMs ?? DEFAULT_WRITE_LATENCY_LIMIT_MS, isGeneralNode: node.isGeneralNode ?? true };
  nodeNetworksText.value = (node.resourceNetworks ?? []).join("\n");
  nodeForm.value.inputRateLimitMiB = node.inputRateLimitMiB ?? 0;
  nodeDialog.value = true;
}

async function saveNode() {
  if (savingNode.value) return;
  if (!nodeForm.value.id.trim() || !nodeForm.value.url.trim()) return ElMessage.warning("请填写节点 ID 和服务地址");
  const resourceNetworks = [...new Set(nodeNetworksText.value.split(/[\s,，]+/).filter(Boolean))];
  if (!nodeForm.value.isGeneralNode && resourceNetworks.length === 0) return ElMessage.warning("非通用节点至少需要一个资源 IP 或网段");
  if (!await confirmAction(`确认保存节点“${nodeForm.value.id.trim()}”的配置吗？`, "确认保存配置")) return;
  savingNode.value = true;
  try {
    if (editingNode.value) {
      await settingsApi.updateNode(editingNode.value.id, {
        version: editingNode.value.version,
        capacity: nodeForm.value.capacity,
        accepting: nodeForm.value.accepting,
        inputRateLimitMiB: nodeForm.value.inputRateLimitMiB ?? 0,
        writeLatencyLimitMs: nodeForm.value.writeLatencyLimitMs ?? DEFAULT_WRITE_LATENCY_LIMIT_MS,
        isGeneralNode: nodeForm.value.isGeneralNode ?? true,
        resourceNetworks,
      });
      ElMessage.success("节点配置已保存");
    } else {
      await settingsApi.registerNode({ ...nodeForm.value, resourceNetworks, id: nodeForm.value.id.trim(), url: nodeForm.value.url.trim() });
      ElMessage.success("节点已登记");
    }
    nodeDialog.value = false;
    await load();
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      ElMessage.warning("节点配置已更新，请确认最新值后重试");
      await load();
    } else ElMessage.error(error instanceof Error ? error.message : "保存节点配置失败");
  } finally {
    savingNode.value = false;
  }
}

async function removeNode(node: NodeConfig) {
  if (deletingNode.value) return;
  // 确认弹窗期间固定目标和版本，后台刷新不能改变即将删除的对象。
  const { id, version } = node;
  deletingNode.value = id;
  try {
    if (!await confirmAction(`确认删除节点“${id}”吗？该节点将不再接收新采集任务，已有日志和历史任务会保留。`, "确认删除节点")) return;
    await settingsApi.deleteNode(id, version);
    ElMessage.success("节点已删除，历史日志已保留");
    await load();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "删除节点失败");
  } finally {
    deletingNode.value = undefined;
  }
}

onMounted(() => void load());
</script>

<template>
  <section class="settings-manager" aria-label="后台配置">
    <div v-if="forbidden" class="settings-forbidden" role="alert"><Settings2 :size="28" /><h2>无后台配置权限</h2><p>当前访问令牌不包含管理员作用域。</p></div>
    <template v-else>
      <div class="settings-toolbar"><div><h2>后台配置</h2><p>平台设置和节点准入配置仅限管理员修改。</p></div><el-tooltip content="刷新后台配置"><el-button circle :icon="RefreshCw" aria-label="刷新后台配置" @click="load" /></el-tooltip></div>
      <nav class="settings-modules" aria-label="后台配置模块">
        <el-radio-group v-model="activeModule" class="settings-module-tabs">
          <el-radio-button value="platform"><SlidersHorizontal :size="15" />平台与容量</el-radio-button>
          <el-radio-button value="nodes"><ServerCog :size="15" />节点准入</el-radio-button>
          <el-radio-button value="monitor"><Gauge :size="15" />资源监控</el-radio-button>
          <el-radio-button value="records"><Database :size="15" />记录保留</el-radio-button>
        </el-radio-group>
      </nav>
      <section v-show="activeModule === 'platform'" class="settings-module" v-loading="loading">
        <header class="module-intro"><h3>平台与容量</h3><p>统一配置归档保留、全局准入和实时日志页面的浏览器内存预算。</p></header>
        <section class="settings-band">
          <div><h4>日志保存天数</h4><p>已发布且未被下载任务保护的归档会由节点维护任务按此天数清理。</p></div>
          <div class="retention-control"><el-input-number v-model="retentionDays" :min="1" :max="3650" controls-position="right" aria-label="日志保存天数" /><span>天</span></div>
        </section>
        <section class="settings-band">
          <div><h4>集群总并发上限</h4><p>所有节点同时运行的采集任务总数上限；节点容量仍会分别限制单节点并发。</p></div>
          <div class="retention-control"><el-input-number v-model="clusterCapacity" :min="1" :max="MAX_CLUSTER_CAPACITY" controls-position="right" aria-label="集群总并发上限" /><span>个任务</span></div>
        </section>
        <section class="settings-band">
          <div><h4>实时日志内存上限</h4><p>每个已登录浏览器实时日志页面可保留的最大日志数据量。超出上限时页面按接收顺序淘汰最早内容，不影响 Worker 原始日志写入。</p></div>
          <div class="retention-control"><el-input-number v-model="liveLogBufferMiB" :min="1" :max="100" :precision="0" controls-position="right" aria-label="实时日志内存上限" /><span>MiB</span></div>
        </section>
        <div class="module-actions"><el-button type="primary" :loading="savingRetention" :disabled="savingRetention || !settings" :icon="Save" @click="savePlatformSettings">保存平台与容量配置</el-button></div>
      </section>
      <section v-show="activeModule === 'nodes'" class="settings-module" v-loading="loading">
        <section class="settings-band node-heading"><div><h3>节点登记与准入</h3><p>已登记节点使用平台准入配置；未登记节点沿用部署配置。登记不会启动节点进程。</p></div><el-button type="primary" :icon="Plus" @click="openRegister()">登记节点</el-button></section>
        <el-alert type="info" :closable="false" show-icon><template #title>在线状态由 worker 心跳计算。离线登记项需要使用相同节点 ID 部署并启动 worker。</template></el-alert>
        <el-table scrollbar-always-on v-loading="loading" :data="nodes" class="data-table settings-table" empty-text="暂无已发现节点">
        <el-table-column label="节点" min-width="190"><template #default="{ row }"><strong>{{ row.id }}</strong><span class="node-url">{{ row.url }}</span></template></el-table-column>
        <el-table-column label="配置" width="110"><template #default="{ row }"><el-tag :type="row.registered ? 'success' : 'warning'">{{ row.registered ? "已登记" : "未登记配置" }}</el-tag></template></el-table-column>
        <el-table-column label="运行状态" width="112"><template #default="{ row }"><el-tag :type="row.online ? 'success' : 'info'">{{ row.online ? "在线" : "离线" }}</el-tag></template></el-table-column>
        <el-table-column label="准入" width="110"><template #default="{ row }"><el-tag v-if="row.registered" :type="row.accepting ? 'success' : 'warning'">{{ row.accepting ? "允许" : "暂停" }}</el-tag><span v-else class="settings-muted">未配置</span></template></el-table-column>
        <el-table-column label="容量" width="100"><template #default="{ row }">{{ row.capacity }}</template></el-table-column>
        <el-table-column label="输入速率上限" width="140"><template #default="{ row }">{{ row.inputRateLimitMiB ? `${row.inputRateLimitMiB} MiB/s` : "未启用" }}</template></el-table-column>
        <el-table-column label="写入延迟上限" width="140"><template #default="{ row }">{{ formatWriteLatencyLimit(row.writeLatencyLimitMs) }}</template></el-table-column>
        <el-table-column label="资源准入" min-width="210"><template #default="{ row }"><el-tag :type="row.isGeneralNode === false ? 'warning' : 'info'">{{ row.isGeneralNode === false ? "专用节点" : "通用节点" }}</el-tag><span v-if="row.isGeneralNode === false" class="node-url">{{ (row.resourceNetworks || []).join('、') }}</span></template></el-table-column>
        <el-table-column label="心跳信息" min-width="180"><template #default="{ row }"><span v-if="row.reportedAt">{{ new Date(row.reportedAt).toLocaleString("zh-CN", { hour12: false }) }}</span><span v-else class="settings-muted">尚未收到心跳</span><span v-if="row.urlMismatch" class="settings-warning">地址与 Worker NODE_URL 不一致</span></template></el-table-column>
        <el-table-column label="操作" width="148" fixed="right"><template #default="{ row }"><el-tooltip :content="row.registered ? '编辑节点准入与容量' : '使用此 Worker 心跳信息登记节点'"><el-button text :icon="row.registered ? Edit3 : Plus" :aria-label="row.registered ? '编辑节点配置' : '登记此节点'" @click="row.registered ? openEdit(row) : openRegister(row)">{{ row.registered ? "编辑" : "登记" }}</el-button></el-tooltip><el-tooltip :content="row.activeTasks ? '节点仍有活动采集任务' : '删除节点，保留历史日志'"><el-button text type="danger" :icon="Trash2" :loading="deletingNode === row.id" :disabled="Boolean(deletingNode) || Boolean(row.activeTasks)" :aria-label="`删除节点 ${row.id}`" @click="removeNode(row)" /></el-tooltip></template></el-table-column>
        </el-table>
      </section>
      <section v-show="activeModule === 'monitor'" class="settings-module"><ResourceMonitorSettings :settings="settings" :loading="loading" @saved="settings = $event" /></section>
      <section v-show="activeModule === 'records'" class="settings-module"><RecordRetentionSettings :settings="settings" :loading="loading" @saved="settings = $event" /></section>
    </template>
    <el-dialog v-model="nodeDialog" :title="editingNode ? '编辑节点配置' : '登记节点'" width="min(560px, 94vw)" class="node-config-dialog" destroy-on-close>
      <el-form label-position="top"><el-form-item label="节点 ID" required><el-input v-model="nodeForm.id" maxlength="128" :disabled="Boolean(editingNode)" /></el-form-item><el-form-item label="Worker 服务地址" required><el-input v-model="nodeForm.url" :disabled="Boolean(editingNode)" placeholder="http://worker:8001" /><p class="node-help">HTTP / HTTPS，须与节点上报地址一致且后端可达。</p></el-form-item><el-form-item label="最大并发任务数"><el-input-number v-model="nodeForm.capacity" :min="1" :max="MAX_NODE_CAPACITY" controls-position="right" aria-label="节点最大并发任务数" /><p class="node-help">登记配置是运行时权威；仅未登记节点使用 Worker 的 NODE_CAPACITY 作为默认容量。</p></el-form-item><el-form-item label="接受新任务"><el-switch v-model="nodeForm.accepting" /></el-form-item></el-form>
      <el-form label-position="top"><el-form-item label="通用节点"><el-switch v-model="nodeForm.isGeneralNode" aria-label="通用节点" /></el-form-item><el-form-item v-if="nodeForm.isGeneralNode === false" label="允许接入的设备资源 IP / CIDR（每行一项）" required><el-input v-model="nodeNetworksText" type="textarea" :rows="4" aria-label="允许接入的设备资源地址" placeholder="10.41.203.35&#10;10.18.117.0/24" /></el-form-item></el-form>
      <el-form label-position="top"><el-form-item label="日志输入速率上限（MiB/s，0 为不限制）"><el-input-number v-model="nodeForm.inputRateLimitMiB" :min="0" :max="100000" :precision="0" aria-label="日志输入速率上限" controls-position="right" /></el-form-item><el-form-item label="写入延迟上限（ms）"><el-input-number v-model="nodeForm.writeLatencyLimitMs" :min="MIN_WRITE_LATENCY_LIMIT_MS" :max="MAX_WRITE_LATENCY_LIMIT_MS" :precision="0" aria-label="写入延迟上限" controls-position="right" /><p class="node-help">超过上限时只暂停新任务准入，不会终止正在采集的会话。</p></el-form-item></el-form>
      <template #footer><el-button @click="nodeDialog = false">取消</el-button><el-button type="primary" :loading="savingNode" :disabled="savingNode" :icon="ServerCog" @click="saveNode">保存配置</el-button></template>
    </el-dialog>
  </section>
</template>

<style scoped>
/* 设置页采用控制台既有的表格与分隔带，避免将配置项包装成多层卡片。 */
.settings-manager { min-width: 0; }
.settings-toolbar, .settings-band, .retention-control { display: flex; align-items: center; gap: 12px; }
.settings-toolbar { justify-content: space-between; margin-bottom: 20px; }
.settings-toolbar h2, .settings-band h3, .settings-band h4, .module-intro h3 { margin: 0 0 5px; }
.settings-modules { overflow-x: auto; padding-bottom: 10px; margin-bottom: 8px; scrollbar-gutter: stable; }
.settings-module-tabs { display: inline-flex; min-width: max-content; }
.settings-module-tabs :deep(.el-radio-button__inner) { display: inline-flex; align-items: center; gap: 7px; min-height: 38px; }
.settings-module { min-width: 0; }
.module-intro { padding: 8px 0 14px; }
.module-intro p, .settings-band p { margin: 0; color: #718084; }
.settings-band { justify-content: space-between; padding: 20px 0; border-top: 1px solid #e5eaeb; }
.settings-band h4 { font-size: 14px; }
.module-actions { display: flex; justify-content: flex-end; padding-top: 18px; border-top: 1px solid #e5eaeb; }
.node-heading { margin-top: 18px; }
.retention-control { flex-shrink: 0; }.settings-table { margin-top: 14px; }
.node-url { display: block; margin-top: 5px; color: #829095; font: 11px ui-monospace, SFMono-Regular, Menlo, monospace; overflow-wrap: anywhere; }
.settings-muted, .node-help { color: #829095; }.settings-warning { display: block; margin-top: 5px; color: #b54708; font-size: 12px; }.node-help { margin: 7px 0 0; font-size: 12px; }.settings-forbidden { display: grid; min-height: 280px; place-items: center; text-align: center; color: #718084; }.settings-forbidden h2 { margin: 14px 0 6px; color: #354548; }.settings-forbidden p { margin: 0; }
:global(.node-config-dialog .el-dialog__body) { max-height: min(62vh, 620px); overflow-y: auto; overscroll-behavior: contain; }
@media (max-width: 700px) { .settings-band { align-items: flex-start; flex-direction: column; }.retention-control { width: 100%; }.retention-control .el-input-number { flex: 1; }.node-heading .el-button, .module-actions .el-button { align-self: stretch; }.module-actions .el-button { width: 100%; }.node-config-dialog :deep(.el-dialog__footer) { display: flex; flex-wrap: wrap; gap: 8px; }.node-config-dialog :deep(.el-dialog__footer .el-button) { flex: 1 1 120px; margin-left: 0; } }
</style>
