<script setup lang="ts">
// 服务令牌始终绑定既有用户；口令仅在查看窗口打开期间保留在页面内存。
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { Copy, Edit3, Eye, KeyRound, Plus, RefreshCw, ShieldCheck, Trash2 } from "lucide-vue-next";
import { ElMessage, ElMessageBox } from "element-plus";
import { ApiError, usersApi, type SessionUser } from "../../shared/api";
import { accessApi, type ServiceToken, type ServiceTokenCreate } from "./api";

const props = withDefaults(defineProps<{ isAdmin?: boolean }>(), { isAdmin: true });

const loading = ref(false);
const creating = ref(false);
const saving = ref(false);
const forbidden = ref(false);
const page = ref(1);
const pageSize = ref(20);
const total = ref(0);
const items = ref<ServiceToken[]>([]);
const users = ref<SessionUser[]>([]);
const formOpen = ref(false);
const secretOpen = ref(false);
const oneTimeToken = ref("");
const revealingId = ref("");
const form = ref<ServiceTokenCreate>({ name: "", userId: "", expiresInDays: 30 });
const permanent = ref(false);
const editing = ref<ServiceToken>();
const editOpen = ref(false);
const editForm = ref({ name: "", userId: "" });
const editExpirationMode = ref<"unchanged" | "permanent" | "days">("unchanged");
const editExpiresInDays = ref(30);
const eligibleUsers = computed(() => users.value.filter(user => user.enabled && !user.deletedAt));
let componentGeneration = 0;

function resetForm() {
  form.value = { name: "", userId: "", expiresInDays: 30 };
  permanent.value = false;
}

async function loadUsers() {
  const items: SessionUser[] = [];
  let page = 1;
  let total = 0;
  do {
    const result = await usersApi.list(page, 100);
    const before = items.length;
    items.push(...result.items);
    total = result.total;
    if (!result.items.length || items.length === before) break;
    page += 1;
  } while (items.length < total);
  return items;
}
async function load() {
  loading.value = true;
  forbidden.value = false;
  try {
    const [data, allUsers] = await Promise.all([accessApi.list(page.value, pageSize.value), props.isAdmin ? loadUsers() : Promise.resolve([])]);
    items.value = data.items;
    total.value = data.total;
    users.value = allUsers;
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
  if (!form.value.userId) return ElMessage.warning("请选择所属用户");
  creating.value = true;
  try {
    const created = await accessApi.create({
      ...form.value,
      name: form.value.name.trim(),
      expiresInDays: permanent.value ? null : form.value.expiresInDays,
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

function openEdit(token: ServiceToken) {
  editing.value = token;
  editForm.value = { name: token.name, userId: token.userId };
  editExpirationMode.value = "unchanged";
  editExpiresInDays.value = 30;
  editOpen.value = true;
}
async function update() {
  if (!editing.value || saving.value) return;
  if (!editForm.value.name.trim() || !editForm.value.userId)
    return ElMessage.warning("请填写令牌名称并选择所属用户");
  const current = { ...editing.value };
  if (editExpirationMode.value === "days" && (!Number.isInteger(editExpiresInDays.value) || editExpiresInDays.value < 1 || editExpiresInDays.value > 365))
    return ElMessage.warning("有效期需为 1 至 365 天");
  const values = {
    name: editForm.value.name.trim(),
    userId: editForm.value.userId,
    expiresInDays: editExpirationMode.value === "unchanged" ? undefined : editExpirationMode.value === "permanent" ? null : editExpiresInDays.value,
  };
  if (current.effectiveStatus === "EXPIRED" && values.expiresInDays !== undefined)
    return ElMessage.warning("已过期令牌不能调整有效期");
  saving.value = true;
  try {
    const message = values.userId !== current.userId
      ? "变更所属用户后，令牌会在下一次请求立即继承新用户的权限。确认继续吗？"
      : values.expiresInDays !== undefined && values.name !== current.name
        ? "确认保存服务令牌的名称和有效期变更吗？"
        : values.expiresInDays !== undefined
          ? "确认保存服务令牌的有效期变更吗？"
          : "确认保存服务令牌的名称变更吗？";
    await ElMessageBox.confirm(message, "确认保存服务令牌", {
      type: "warning", confirmButtonText: "确认保存", cancelButtonText: "取消",
    });
    await accessApi.update(current.id, {
      version: current.version,
      ...values,
    });
    editOpen.value = false;
    await load();
    ElMessage.success("服务令牌已更新");
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      editOpen.value = false;
      await load();
      ElMessage.warning(error.message || "无法保存服务令牌，已刷新最新列表");
    } else if (error !== "cancel" && error !== "close")
      ElMessage.error(error instanceof Error ? error.message : "更新服务令牌失败");
  } finally {
    saving.value = false;
  }
}

function tokenStatus(token: ServiceToken) {
  const labels = {
    ACTIVE: ["有效", "success"], REVOKED: ["已撤销", "info"], EXPIRED: ["已过期", "warning"],
    USER_DISABLED: ["所属用户已停用", "warning"], USER_DELETED: ["所属用户已删除", "danger"], USER_MISSING: ["所属用户不存在", "danger"],
  } as const;
  const [label, type] = labels[token.effectiveStatus] ?? labels.ACTIVE;
  return { label, type };
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

async function reveal(token: ServiceToken) {
  if (revealingId.value) return;
  const generation = componentGeneration;
  revealingId.value = token.id;
  try {
    const revealed = await accessApi.reveal(token.id);
    if (generation !== componentGeneration) return;
    oneTimeToken.value = revealed.token;
    secretOpen.value = true;
  } catch (error) {
    if (generation !== componentGeneration) return;
    if (error instanceof ApiError && error.code === "SERVICE_TOKEN_LEGACY_SECRET" && props.isAdmin) {
      try {
        await ElMessageBox.confirm("该服务账号的旧口令仅保留了不可还原的散列值。重新生成会立即使旧口令失效。", "旧口令无法查看", {
          type: "warning", confirmButtonText: "重新生成", cancelButtonText: "取消",
        });
        if (generation !== componentGeneration) return;
        const rotated = await accessApi.rotate(token.id, token.version);
        if (generation !== componentGeneration) return;
        oneTimeToken.value = rotated.token;
        secretOpen.value = true;
        await load();
      } catch (rotateError) {
        if (rotateError !== "cancel" && rotateError !== "close")
          ElMessage.error(rotateError instanceof Error ? rotateError.message : "重新生成服务账号口令失败");
      }
    } else {
      if (error instanceof ApiError && error.status === 409) await load();
      ElMessage.error(error instanceof Error ? error.message : "查看服务账号口令失败");
    }
  } finally {
    revealingId.value = "";
  }
}

async function copyToken() {
  try {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(oneTimeToken.value);
    else {
      const fallback = document.createElement("textarea");
      fallback.value = oneTimeToken.value;
      fallback.setAttribute("readonly", "");
      fallback.style.position = "fixed";
      fallback.style.opacity = "0";
      document.body.append(fallback);
      fallback.select();
      const copied = document.execCommand("copy");
      fallback.remove();
      if (!copied) throw new Error("浏览器拒绝写入剪贴板");
    }
    ElMessage.success("口令已复制");
  } catch {
    ElMessage.error("复制失败，请手动复制口令");
  }
}

function closeSecret() {
  oneTimeToken.value = "";
}

onMounted(() => void load());
onBeforeUnmount(() => {
  componentGeneration += 1;
  secretOpen.value = false;
  oneTimeToken.value = "";
});
</script>

<template>
  <section class="access-manager" aria-label="服务账号管理">
    <template v-if="forbidden">
      <div class="access-forbidden" role="alert">
        <ShieldCheck :size="28" />
        <h2>无服务账号查看权限</h2>
        <p>当前访问令牌不包含服务账号查看权限。</p>
      </div>
    </template>
    <template v-else>
      <div class="access-toolbar">
        <div><h2>{{ props.isAdmin ? "第三方服务账号" : "我的服务账号" }}</h2><p>{{ props.isAdmin ? "可查看当前可恢复口令；旧口令无法还原时可重新生成。" : "可查看并复制分配给当前账号的服务口令。" }}</p></div>
        <div class="access-actions">
          <el-tooltip content="刷新服务账号列表"><el-button :icon="RefreshCw" circle aria-label="刷新服务账号列表" @click="load" /></el-tooltip>
          <el-button v-if="props.isAdmin" type="primary" :icon="Plus" @click="openCreate">新建服务账号</el-button>
        </div>
      </div>
      <el-table scrollbar-always-on v-loading="loading" :data="items" class="data-table" empty-text="暂无服务账号">
        <el-table-column label="名称" min-width="180"><template #default="{ row }"><strong>{{ row.name }}</strong><span class="access-id">{{ row.id }}</span></template></el-table-column>
        <el-table-column v-if="props.isAdmin" label="所属用户" min-width="180"><template #default="{ row }"><strong>{{ row.user?.displayName || "用户已不可用" }}</strong><span class="access-id">{{ row.user?.username || row.userId }}</span></template></el-table-column>
        <el-table-column label="有效期" min-width="160"><template #default="{ row }">{{ row.expiresAt ? new Date(row.expiresAt).toLocaleString("zh-CN", { hour12: false }) : "永久" }}</template></el-table-column>
        <el-table-column label="状态" width="100"><template #default="{ row }"><el-tag :type="tokenStatus(row).type">{{ tokenStatus(row).label }}</el-tag></template></el-table-column>
        <el-table-column label="操作" :width="props.isAdmin ? 150 : 58" fixed="right"><template #default="{ row }"><el-tooltip content="查看服务账号口令"><el-button text :icon="Eye" :loading="revealingId === row.id" :disabled="Boolean(revealingId)" aria-label="查看服务账号口令" @click="reveal(row)" /></el-tooltip><template v-if="props.isAdmin"><el-tooltip content="编辑服务账号"><el-button text :icon="Edit3" aria-label="编辑服务账号" @click="openEdit(row)" /></el-tooltip><el-tooltip :content="row.revoked ? '账号已撤销' : '撤销账号'"><el-button text type="danger" :icon="Trash2" :disabled="row.revoked" aria-label="撤销服务账号" @click="revoke(row)" /></el-tooltip></template></template></el-table-column>
      </el-table>
      <el-pagination v-model:current-page="page" v-model:page-size="pageSize" :total="total" :page-sizes="[20, 50, 100]" layout="total, prev, pager, next" @change="load" />
    </template>

    <el-dialog v-model="formOpen" title="新建第三方服务账号" width="min(560px, 94vw)" destroy-on-close>
      <el-form label-position="top">
        <el-form-item label="账号名称" required><el-input v-model="form.name" maxlength="128" /></el-form-item>
        <el-form-item label="有效期"><el-switch v-model="permanent" active-text="永久有效" /><template v-if="!permanent"><el-input-number v-model="form.expiresInDays" :min="1" :max="365" controls-position="right" /><span class="access-suffix">天</span></template></el-form-item>
        <el-form-item label="所属用户" required><el-select v-model="form.userId" filterable placeholder="选择有效用户" style="width: 100%"><el-option v-for="user in eligibleUsers" :key="user.id" :label="`${user.displayName} · ${user.username}`" :value="user.id" /></el-select><p class="access-muted">默认包含日志与 Coredump 查询、导出和下载。令牌实时继承该用户权限，仍受来源 IP 策略约束；停用用户会立即使令牌失效。</p></el-form-item>
      </el-form>
      <template #footer><el-button @click="formOpen = false">取消</el-button><el-button type="primary" :loading="creating" :disabled="creating" :icon="KeyRound" @click="create">创建并显示口令</el-button></template>
    </el-dialog>

    <el-dialog v-model="editOpen" title="编辑第三方服务账号" width="min(560px, 94vw)" :close-on-click-modal="!saving">
      <el-form label-position="top"><el-form-item label="账号名称" required><el-input v-model="editForm.name" maxlength="128" :disabled="saving" /></el-form-item><el-form-item label="所属用户" required><el-select v-model="editForm.userId" filterable :disabled="saving" style="width: 100%"><el-option v-for="user in eligibleUsers" :key="user.id" :label="`${user.displayName} · ${user.username}`" :value="user.id" /></el-select></el-form-item><el-form-item label="有效期"><el-radio-group v-model="editExpirationMode" :disabled="saving || editing?.effectiveStatus === 'EXPIRED'"><el-radio value="unchanged">保持不变</el-radio><el-radio value="permanent">永久</el-radio><el-radio value="days">期限</el-radio></el-radio-group><template v-if="editExpirationMode === 'days'"><el-input-number v-model="editExpiresInDays" :min="1" :max="365" controls-position="right" :disabled="saving || editing?.effectiveStatus === 'EXPIRED'" /><span class="access-suffix">天</span></template><p class="access-muted">编辑不会恢复已撤销或过期的令牌。</p></el-form-item></el-form>
      <template #footer><el-button :disabled="saving" @click="editOpen = false">取消</el-button><el-button type="primary" :loading="saving" @click="update">保存</el-button></template>
    </el-dialog>

    <el-dialog v-model="secretOpen" title="服务账号口令" width="min(640px, 94vw)" :close-on-click-modal="false" @closed="closeSecret">
      <p class="access-secret-note">请妥善保管口令；关闭此窗口会从当前页面内存清除。</p>
      <el-input :model-value="oneTimeToken" readonly aria-label="服务账号口令"><template #append><el-button :icon="Copy" aria-label="复制口令" @click="copyToken">复制</el-button></template></el-input>
      <template #footer><el-button type="primary" @click="secretOpen = false">关闭</el-button></template>
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
