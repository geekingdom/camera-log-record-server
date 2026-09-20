<script setup lang="ts">
// 批量新增只处理海康网络设备；逐行凭据可调整，认证与最终原子保存均由服务端再次校验。
import { computed, onBeforeUnmount, ref } from "vue";
import { ElMessage } from "element-plus";
import { KeyRound, Trash2 } from "lucide-vue-next";
import { ApiError, api, idempotencyKey } from "../../shared/api";
import { confirmAction } from "../../shared/confirm";
import { createBulkResourceDraft, type BatchResourceRow } from "./bulkResource";
import type { ResourceAuthType } from "../../shared/types";

const props = defineProps<{ disabled?: boolean }>();
const emit = defineEmits<{ saved: []; processing: [value: boolean] }>();
const draft = createBulkResourceDraft();
const startIp = ref("");
const endIp = ref("");
const credentials = ref<{ username: string; password: string; authType: ResourceAuthType }>({ username: "", password: "", authType: "DIGEST" });
const enableCoredumpMonitor = ref(false);
const enableResourceMonitor = ref(false);
const saving = ref(false);
const configuring = ref(false);
const submittingKey = ref("");
const appliedRange = ref("");
let mounted = true;
const configurationDirty = computed(() => credentials.value.username !== draft.shared.username
  || credentials.value.password !== draft.shared.password || credentials.value.authType !== draft.shared.authType
  || `${startIp.value.trim()}-${endIp.value.trim()}` !== appliedRange.value);
const canSubmit = computed(() => !configurationDirty.value && draft.rows.length > 0 && draft.rows.every(row => row.authentication === "SUCCESS"));
const locked = computed(() => Boolean(props.disabled || saving.value || configuring.value || draft.authenticating.value));
onBeforeUnmount(() => { mounted = false; draft.cancelAuthentication(); });

function resetRequestKey() { submittingKey.value = ""; }
async function commitCredentials() {
  const changed = credentials.value.username !== draft.shared.username
    || credentials.value.password !== draft.shared.password || credentials.value.authType !== draft.shared.authType;
  if (!changed) return true;
  if (draft.rows.length) {
    configuring.value = true;
    try {
      if (!(await confirmAction("将覆盖当前所有行的 HTTP 用户名、密码和认证方式，并要求重新认证。", "更新共用认证配置"))) {
        credentials.value = { ...draft.shared };
        return false;
      }
    } finally { configuring.value = false; }
    if (!mounted) return false;
  }
  draft.setShared(credentials.value);
  resetRequestKey();
  return true;
}
async function generateRows() {
  if (!(await commitCredentials())) return;
  if (!mounted) return false;
  const range = `${startIp.value.trim()}-${endIp.value.trim()}`;
  if (draft.rows.length && range !== appliedRange.value) {
    configuring.value = true;
    try {
      if (!(await confirmAction("将替换当前范围内的逐行名称、凭据和认证结果，是否继续？", "替换批量设备范围"))) return false;
    } finally { configuring.value = false; }
    if (!mounted) return false;
  }
  try {
    draft.applyRange(startIp.value, endIp.value);
    appliedRange.value = `${startIp.value.trim()}-${endIp.value.trim()}`;
    resetRequestKey();
    return true;
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : "IP 范围不合法"); }
  return false;
}
function updateRow(index: number, value: Partial<Pick<BatchResourceRow, "name" | "username" | "password" | "authType">>) {
  draft.updateRow(index, value);
  resetRequestKey();
}
async function authenticate(row?: number) {
  if (locked.value) return;
  const send = (value: Pick<BatchResourceRow, "ip" | "name" | "username" | "password" | "authType">) => api.authenticateResource({
    name: value.name || value.ip, kind: "HIKVISION_NETWORK", ip: value.ip,
    username: value.username, password: value.password, authType: value.authType,
  });
  try {
    if (row === undefined) {
      const range = `${startIp.value.trim()}-${endIp.value.trim()}`;
      if (!draft.rows.length || range !== appliedRange.value) {
        if (!(await generateRows())) return;
      }
      await draft.authenticateAll(send);
    }
    else await draft.authenticateRow(row, send);
    if (draft.rows.some(value => value.authentication === "FAILED")) ElMessage.warning("存在认证失败设备，请修改后重试");
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : "批量认证失败"); }
}
async function removeRow(index: number) {
  const row = draft.rows[index];
  if (!row || locked.value) return;
  if (!(await confirmAction(`确认从本批中删除设备 ${row.ip}？`, "删除批量设备"))) return;
  draft.remove(index);
  resetRequestKey();
}
function status(row: BatchResourceRow) {
  return { IDLE: "未认证", AUTHENTICATING: "认证中", SUCCESS: "认证成功", FAILED: row.error || "认证失败" }[row.authentication];
}
function invalidateServerFailures(error: unknown) {
  const details = error instanceof ApiError ? error.details : undefined;
  const values = Array.isArray(details) ? details : Array.isArray((details as { items?: unknown[] } | undefined)?.items)
    ? (details as { items: unknown[] }).items : [];
  for (const detail of values) {
    const value = detail as { ip?: string; message?: string; error?: string };
    const row = draft.rows.find(item => item.ip === value.ip);
    if (row) { row.authentication = "FAILED"; row.error = value.message ?? value.error ?? "服务端认证失败"; }
  }
}
async function submit() {
  if (locked.value || !canSubmit.value) return;
  saving.value = true;
  emit("processing", true);
  const key = submittingKey.value || idempotencyKey();
  submittingKey.value = key;
  try {
    const response = await api.createResourcesBatch(draft.rows.map(row => ({
      name: row.name.trim() || row.ip, kind: "HIKVISION_NETWORK", ip: row.ip, username: row.username,
      password: row.password, authType: row.authType, enableCoredumpMonitor: enableCoredumpMonitor.value,
      enableResourceMonitor: enableResourceMonitor.value,
    })), key);
    if (response.items.length !== draft.rows.length) throw new Error("服务端未返回完整批量保存结果");
    if (!mounted) return;
    ElMessage.success(`已添加 ${response.items.length} 台设备`);
    emit("saved");
  } catch (error) {
    if (!mounted) return;
    invalidateServerFailures(error);
    ElMessage.error(error instanceof Error ? error.message : "批量添加失败，可使用原配置重试");
  } finally {
    saving.value = false;
    if (mounted) emit("processing", false);
  }
}
</script>

<template>
  <section class="resource-batch-editor">
    <el-form label-position="top" :disabled="locked">
      <div class="form-grid batch-range-grid">
        <el-form-item label="起始 IP"><el-input v-model="startIp" aria-label="起始 IP" /></el-form-item>
        <el-form-item label="结束 IP"><el-input v-model="endIp" aria-label="结束 IP" /></el-form-item>
        <el-form-item label="HTTP 用户名"><el-input v-model="credentials.username" aria-label="批量用户名" autocomplete="off" @change="commitCredentials" /></el-form-item>
        <el-form-item label="HTTP 密码"><el-input v-model="credentials.password" aria-label="批量密码" type="password" show-password autocomplete="new-password" @change="commitCredentials" /></el-form-item>
        <el-form-item label="认证方式"><el-select v-model="credentials.authType" @change="commitCredentials"><el-option label="摘要认证" value="DIGEST" /><el-option label="基础认证" value="BASIC" /></el-select></el-form-item>
      </div>
      <div class="batch-actions"><el-button type="primary" :icon="KeyRound" @click="authenticate()">批量认证</el-button><span>点击后生成范围内 IP 并认证；最多 {{ 256 }} 台，认证成功后才能添加。</span></div>
      <div class="resource-monitor-options batch-monitor-options">
        <div><strong>Coredump 监控</strong><small>应用到本批全部设备</small></div><el-switch v-model="enableCoredumpMonitor" aria-label="批量启用 Coredump 监控" @change="resetRequestKey" />
        <div><strong>CPU 与内存监控</strong><small>应用到本批全部设备</small></div><el-switch v-model="enableResourceMonitor" aria-label="批量启用 CPU 与内存监控" @change="resetRequestKey" />
      </div>
    </el-form>
    <el-table :data="draft.rows" aria-label="批量设备认证结果" max-height="420" class="batch-resource-table">
      <el-table-column type="index" label="序号" width="64" />
      <el-table-column label="资源名称" min-width="160"><template #default="{ $index, row }"><el-input :model-value="row.name" :aria-label="`资源名称 ${row.ip}`" :disabled="locked" @update:model-value="updateRow($index, { name: String($event) })" /></template></el-table-column>
      <el-table-column prop="ip" label="设备 IP" min-width="150" />
      <el-table-column label="用户名" min-width="130"><template #default="{ $index, row }"><el-input :model-value="row.username" :aria-label="`用户名 ${row.ip}`" :disabled="locked" @update:model-value="updateRow($index, { username: String($event) })" /></template></el-table-column>
      <el-table-column label="密码" min-width="150"><template #default="{ $index, row }"><el-input :model-value="row.password" :aria-label="`密码 ${row.ip}`" :disabled="locked" type="password" show-password @update:model-value="updateRow($index, { password: String($event) })" /></template></el-table-column>
      <el-table-column label="认证结果" min-width="130"><template #default="{ row }"><el-tag :type="row.authentication === 'SUCCESS' ? 'success' : row.authentication === 'FAILED' ? 'danger' : row.authentication === 'AUTHENTICATING' ? 'warning' : 'info'">{{ status(row) }}</el-tag></template></el-table-column>
      <el-table-column label="操作" width="132" fixed="right"><template #default="{ $index, row }"><el-tooltip content="立即认证"><el-button text :icon="KeyRound" :aria-label="`立即认证 ${row.ip}`" :disabled="locked" @click="authenticate($index)" /></el-tooltip><el-tooltip content="删除"><el-button text type="danger" :icon="Trash2" :aria-label="`删除 ${row.ip}`" :disabled="locked" @click="removeRow($index)" /></el-tooltip></template></el-table-column>
    </el-table>
    <div class="batch-submit"><el-button type="primary" :loading="saving" :disabled="!canSubmit || locked" @click="submit">添加设备</el-button></div>
  </section>
</template>

<style scoped>
.batch-actions, .batch-submit { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.batch-actions { margin: 4px 0 16px; color: var(--el-text-color-secondary); font-size: 13px; }
.batch-monitor-options { margin: 16px 0; }
.resource-monitor-options { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 12px 16px; align-items: center; }
.resource-monitor-options small { display: block; margin-top: 4px; color: var(--el-text-color-secondary); }
.batch-resource-table { width: 100%; }
.batch-submit { justify-content: flex-end; margin-top: 16px; }
@media (max-width: 700px) { .batch-resource-table { font-size: 13px; } }
</style>
