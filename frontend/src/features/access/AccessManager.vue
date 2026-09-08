<script setup lang="ts">
// 服务账号管理独立维护分页、创建和撤销状态，明文口令只存于一次性对话框状态。
import { computed, onMounted, ref } from "vue";
import { Copy, KeyRound, Plus, RefreshCw, ShieldCheck, Trash2 } from "lucide-vue-next";
import { ElMessage, ElMessageBox } from "element-plus";
import { ApiError } from "../../shared/api";
import { accessApi, type ServiceToken, type ServiceTokenCreate } from "./api";

const scopeOptions = [
  "tasks:read", "tasks:write", "tasks:control", "commands:send", "templates:read",
  "templates:write", "logs:read", "logs:download",
];
const loading = ref(false);
const creating = ref(false);
const forbidden = ref(false);
const page = ref(1);
const pageSize = ref(20);
const total = ref(0);
const items = ref<ServiceToken[]>([]);
const formOpen = ref(false);
const secretOpen = ref(false);
const oneTimeToken = ref("");
const form = ref<ServiceTokenCreate>({ name: "", scopes: [], expiresInDays: 30 });
const taskIdsInput = ref("");
const taskIds = computed(() =>
  taskIdsInput.value.split(/[\s,]+/).map((value) => value.trim()).filter(Boolean),
);

function resetForm() {
  form.value = { name: "", scopes: [], expiresInDays: 30 };
  taskIdsInput.value = "";
}

async function load() {
  loading.value = true;
  forbidden.value = false;
  try {
    const data = await accessApi.list(page.value, pageSize.value);
    items.value = data.items;
    total.value = data.total;
  } catch (error) {
    if (error instanceof ApiError && error.status === 403) forbidden.value = true;
    else ElMessage.error(error instanceof Error ? error.message : "读取服务账号失败");
  } finally {
    loading.value = false;
  }
}

function openCreate() {
  resetForm();
  formOpen.value = true;
}

async function create() {
  if (creating.value) return;
  if (!form.value.name.trim()) return ElMessage.warning("请输入账号名称");
  if (!form.value.scopes.length) return ElMessage.warning("请至少选择一项权限");
  creating.value = true;
  try {
    const created = await accessApi.create({
      ...form.value,
      name: form.value.name.trim(),
      taskIds: taskIds.value.length ? taskIds.value : undefined,
    });
    formOpen.value = false;
    oneTimeToken.value = created.token;
    secretOpen.value = true;
    await load();
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "创建服务账号失败");
  } finally {
    creating.value = false;
  }
}

function tokenStatus(token: ServiceToken) {
  if (token.revoked) return { label: "已撤销", type: "info" as const };
  if (new Date(token.expiresAt).getTime() <= Date.now()) return { label: "已过期", type: "warning" as const };
  return { label: "有效", type: "success" as const };
}

async function revoke(token: ServiceToken) {
  try {
    await ElMessageBox.confirm(`撤销服务账号“${token.name}”后，使用该口令的请求将立即失效。`, "确认撤销", {
      confirmButtonText: "撤销账号",
      cancelButtonText: "取消",
      type: "warning",
    });
    await accessApi.revoke(token.id);
    ElMessage.success("服务账号已撤销");
    await load();
  } catch (error) {
    if (error !== "cancel" && error !== "close")
      ElMessage.error(error instanceof Error ? error.message : "撤销服务账号失败");
  }
}

async function copyToken() {
  try {
    await navigator.clipboard.writeText(oneTimeToken.value);
    ElMessage.success("口令已复制");
  } catch {
    ElMessage.error("复制失败，请手动复制口令");
  }
}

function closeSecret() {
  oneTimeToken.value = "";
}

onMounted(() => void load());
</script>

<template>
  <section class="access-manager" aria-label="服务账号管理">
    <template v-if="forbidden">
      <div class="access-forbidden" role="alert">
        <ShieldCheck :size="28" />
        <h2>无服务账号管理权限</h2>
        <p>当前访问令牌不包含管理员作用域。</p>
      </div>
    </template>
    <template v-else>
      <div class="access-toolbar">
        <div><h2>第三方服务账号</h2><p>口令仅在创建后显示一次。</p></div>
        <div class="access-actions">
          <el-tooltip content="刷新服务账号列表"><el-button :icon="RefreshCw" circle aria-label="刷新服务账号列表" @click="load" /></el-tooltip>
          <el-button type="primary" :icon="Plus" @click="openCreate">新建服务账号</el-button>
        </div>
      </div>
      <el-table v-loading="loading" :data="items" class="data-table" empty-text="暂无服务账号">
        <el-table-column label="名称" min-width="180"><template #default="{ row }"><strong>{{ row.name }}</strong><span class="access-id">{{ row.id }}</span></template></el-table-column>
        <el-table-column label="权限" min-width="220"><template #default="{ row }"><el-tag v-for="scope in row.scopes" :key="scope" effect="plain">{{ scope }}</el-tag></template></el-table-column>
        <el-table-column label="设备范围" min-width="180"><template #default="{ row }"><span v-if="row.taskIds?.length">{{ row.taskIds.join("、") }}</span><span v-else class="access-muted">全部设备</span></template></el-table-column>
        <el-table-column label="有效期" min-width="160"><template #default="{ row }">{{ new Date(row.expiresAt).toLocaleString("zh-CN", { hour12: false }) }}</template></el-table-column>
        <el-table-column label="状态" width="100"><template #default="{ row }"><el-tag :type="tokenStatus(row).type">{{ tokenStatus(row).label }}</el-tag></template></el-table-column>
        <el-table-column label="操作" width="84" fixed="right"><template #default="{ row }"><el-tooltip :content="row.revoked ? '账号已撤销' : '撤销账号'"><el-button text type="danger" :icon="Trash2" :disabled="row.revoked" aria-label="撤销服务账号" @click="revoke(row)" /></el-tooltip></template></el-table-column>
      </el-table>
      <el-pagination v-model:current-page="page" v-model:page-size="pageSize" :total="total" :page-sizes="[20, 50, 100]" layout="total, prev, pager, next" @change="load" />
    </template>

    <el-dialog v-model="formOpen" title="新建第三方服务账号" width="min(560px, 94vw)" destroy-on-close>
      <el-form label-position="top">
        <el-form-item label="账号名称" required><el-input v-model="form.name" maxlength="128" /></el-form-item>
        <el-form-item label="有效期"><el-input-number v-model="form.expiresInDays" :min="1" :max="365" controls-position="right" /><span class="access-suffix">天</span></el-form-item>
        <el-form-item label="权限" required><el-select v-model="form.scopes" multiple filterable placeholder="选择服务账号权限" style="width: 100%"><el-option v-for="scope in scopeOptions" :key="scope" :label="scope" :value="scope" /></el-select></el-form-item>
        <el-form-item label="限定任务 ID"><el-input v-model="taskIdsInput" type="textarea" :rows="3" placeholder="留空可访问全部任务；多个 ID 使用逗号或换行分隔" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="formOpen = false">取消</el-button><el-button type="primary" :loading="creating" :disabled="creating" :icon="KeyRound" @click="create">创建并显示口令</el-button></template>
    </el-dialog>

    <el-dialog v-model="secretOpen" title="请立即保存服务账号口令" width="min(640px, 94vw)" :close-on-click-modal="false" @closed="closeSecret">
      <p class="access-secret-note">此口令不会再次显示，也不会保存到列表。</p>
      <el-input :model-value="oneTimeToken" readonly aria-label="一次性服务账号口令"><template #append><el-button :icon="Copy" aria-label="复制口令" @click="copyToken">复制</el-button></template></el-input>
      <template #footer><el-button type="primary" @click="secretOpen = false">我已保存口令</el-button></template>
    </el-dialog>
  </section>
</template>

<style scoped>
/* 本模块沿用控制台表格与表单视觉，仅补充服务账号信息的紧凑排版。 */
.access-manager { min-width: 0; }
.access-toolbar, .access-actions { display: flex; align-items: center; gap: 12px; }
.access-toolbar { justify-content: space-between; margin-bottom: 20px; }
.access-toolbar h2 { margin: 0 0 5px; }
.access-id { display: block; margin-top: 5px; color: #9ca6aa; font: 10px ui-monospace, SFMono-Regular, Menlo, monospace; overflow: hidden; text-overflow: ellipsis; }
.data-table .el-tag + .el-tag { margin-left: 4px; }
.access-muted, .access-secret-note { color: #829095; }
.access-suffix { margin-left: 8px; color: #6d7a7e; }
.access-forbidden { display: grid; place-items: center; min-height: 280px; text-align: center; color: #718084; }
.access-forbidden h2 { margin: 14px 0 6px; color: #354548; }
.access-forbidden p { margin: 0; }
@media (max-width: 700px) { .access-toolbar { align-items: flex-start; } .access-actions { flex-shrink: 0; gap: 6px; } }
</style>
