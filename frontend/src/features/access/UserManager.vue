<script setup lang="ts">
// 用户账号管理：基础查看、下载和模板权限固定有效，仅维护额外功能权限及用户密码。
import { computed, onMounted, ref } from "vue";
import { Edit3, KeyRound, Plus, RefreshCw, Trash2 } from "lucide-vue-next";
import { ElMessage } from "element-plus";
import { ApiError, usersApi, type SessionUser } from "../../shared/api";
import { userSaveFailureMessage } from "./userManagerMessages";

const baseScopes = ["tasks:read", "logs:read", "logs:download", "templates:read", "templates:write", "service-tokens:read"];
const baseScopeLabels: Record<string, string> = {
  "tasks:read": "查看设备与任务",
  "logs:read": "查看日志与 Coredump",
  "logs:download": "下载日志与 Coredump",
  "templates:read": "查看命令模板",
  "templates:write": "创建命令模板",
  "service-tokens:read": "查看我的服务账号",
};
const users = ref<SessionUser[]>([]);
const scopes = ref<{ value: string; label: string }[]>([]);
const loading = ref(false);
const saving = ref(false);
const resetting = ref(false);
const loadError = ref("");
const open = ref(false);
const resetOpen = ref(false);
const deleteOpen = ref(false);
const deleting = ref(false);
const deletingUser = ref<SessionUser>();
const resourceAction = ref<"DELETE" | "TRANSFER" | "">("");
const targetUserId = ref("");
const transferTargets = ref<Pick<SessionUser, "id" | "username" | "displayName">[]>([]);
let deleteRequestGeneration = 0;
const editing = ref<SessionUser>();
const form = ref({
  username: "",
  displayName: "",
  password: "",
  scopes: [] as string[],
  enabled: true,
});
const resetPassword = ref("");
const pageNumber = ref(1);
const total = ref(0);

// 加载列表及可授权项，失败时保留重试入口，避免将空列表误展示为无用户。
async function load() {
  loading.value = true;
  loadError.value = "";
  try {
    const [page, permissions] = await Promise.all([
      usersApi.list(pageNumber.value, 20),
      usersApi.permissions(),
    ]);
    users.value = page.items;
    total.value = page.total;
    scopes.value = permissions.scopes.filter(scope => !baseScopes.includes(scope.value));
  } catch (error) {
    loadError.value =
      error instanceof Error ? error.message : "读取用户账号失败";
  } finally {
    loading.value = false;
  }
}

function resetForm() {
  form.value = {
    username: "",
    displayName: "",
    password: "",
    scopes: [],
    enabled: true,
  };
}
function openCreate() {
  editing.value = undefined;
  resetForm();
  open.value = true;
}
function openEdit(user: SessionUser) {
  editing.value = user;
  form.value = {
    username: user.username,
    displayName: user.displayName,
    password: "",
    scopes: [...user.scopes],
    enabled: user.enabled,
  };
  open.value = true;
}
function closeEditor() {
  open.value = false;
  form.value.password = "";
}
function closeReset() {
  resetOpen.value = false;
  resetPassword.value = "";
}
const eligibleTransferTargets = computed(() => transferTargets.value.filter(item => item.id !== deletingUser.value?.id));
async function openDelete(user: SessionUser) {
  const generation = ++deleteRequestGeneration;
  deletingUser.value = user;
  resourceAction.value = "";
  targetUserId.value = "";
  transferTargets.value = [];
  try {
    const first = await usersApi.shareTargets(1, 100);
    const pages = await Promise.all(Array.from({ length: Math.ceil(first.total / 100) - 1 }, (_, index) => usersApi.shareTargets(index + 2, 100)));
    if (generation !== deleteRequestGeneration) return;
    transferTargets.value = [first.items, ...pages.map(page => page.items)].flat();
    deleteOpen.value = true;
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "读取可转移用户失败");
  }
}
function closeDelete() {
  deleteRequestGeneration += 1;
  deleteOpen.value = false;
  deletingUser.value = undefined;
  targetUserId.value = "";
}
function displayScopes(user: SessionUser) {
  return user.isAdmin ? ["*"] : [...baseScopes, ...user.scopes];
}
async function reloadAfterConflict(message: string) {
  await load();
  ElMessage.warning(message);
}

// 创建和更新共用表单；saving 覆盖整个请求周期，避免同一版本被重复提交。
async function save() {
  if (!form.value.displayName.trim())
    return ElMessage.warning("请填写显示名称");
  if (
    !editing.value &&
    (form.value.username.trim().length < 1 ||
      form.value.password.length < 8 ||
      form.value.password.length > 128)
  )
    return ElMessage.warning("请填写用户名，初始密码需为 8 至 128 位");
  saving.value = true;
  try {
    if (editing.value)
      await usersApi.update(editing.value.id, {
        displayName: form.value.displayName.trim(),
        scopes: form.value.scopes,
        enabled: form.value.enabled,
        version: editing.value.version ?? 1,
      });
    else
      await usersApi.create({
        username: form.value.username.trim(),
        displayName: form.value.displayName.trim(),
        password: form.value.password,
        isAdmin: false,
        scopes: form.value.scopes,
        enabled: true,
      });
    closeEditor();
    await load();
    ElMessage.success("用户已保存");
  } catch (error) {
    if (editing.value && error instanceof ApiError && error.status === 409)
      await reloadAfterConflict("用户已被其他管理员修改，已刷新最新列表");
    else
      ElMessage.error(userSaveFailureMessage(error, Boolean(editing.value)));
  } finally {
    form.value.password = "";
    saving.value = false;
  }
}

async function disable() {
  const user = deletingUser.value;
  if (!user) return;
  if (!resourceAction.value)
    return ElMessage.warning("请选择资源处置方式");
  if (resourceAction.value === "TRANSFER" && !targetUserId.value)
    return ElMessage.warning("请选择资源接收用户");
  try {
    deleting.value = true;
    await usersApi.disable(user.id, user.version ?? 1, resourceAction.value, targetUserId.value || undefined);
    closeDelete();
    await load();
    ElMessage.success("用户已删除");
  } catch (error) {
    if (error instanceof ApiError && error.status === 409)
      await reloadAfterConflict("用户已被其他管理员修改，已刷新最新列表");
    else ElMessage.error(error instanceof Error ? error.message : "删除用户失败");
  } finally {
    deleting.value = false;
  }
}
function openReset(user: SessionUser) {
  editing.value = user;
  resetPassword.value = "";
  resetOpen.value = true;
}
// 密码重置后立即清空值，关闭弹窗同样清空，避免密码残留在组件状态中。
async function reset(user: SessionUser) {
  if (resetPassword.value.length < 8 || resetPassword.value.length > 128)
    return ElMessage.warning("新密码需为 8 至 128 位");
  resetting.value = true;
  try {
    await usersApi.resetPassword(
      user.id,
      resetPassword.value,
      user.version ?? 1,
    );
    closeReset();
    ElMessage.success("密码已重置");
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      closeReset();
      await reloadAfterConflict("用户已被其他管理员修改，已刷新最新列表");
    } else
      ElMessage.error(error instanceof Error ? error.message : "重置密码失败");
  } finally {
    resetPassword.value = "";
    resetting.value = false;
  }
}
onMounted(() => void load());
</script>

<template>
  <section class="user-manager">
    <div class="access-toolbar">
      <h2>用户账号</h2>
      <div class="toolbar-actions">
        <el-button
          :icon="RefreshCw"
          circle
          aria-label="刷新用户"
          :loading="loading"
          @click="load"
        /><el-button
          type="primary"
          :icon="Plus"
          :disabled="loading"
          @click="openCreate"
          >新建用户</el-button
        >
      </div>
    </div>
    <el-alert
      v-if="loadError"
      type="error"
      :title="loadError"
      show-icon
      :closable="false"
      class="load-error"
      ><template #default
        ><el-button text type="primary" @click="load">重试</el-button></template
      ></el-alert
    >
    <el-table v-loading="loading" :data="users" class="data-table">
      <el-table-column label="用户" min-width="170"
        ><template #default="{ row }"
          ><strong>{{ row.displayName }}</strong
          ><span class="access-id">{{ row.username }}</span></template
        ></el-table-column
      >
      <el-table-column label="权限" min-width="210"
        ><template #default="{ row }"
          ><el-tag v-for="scope in displayScopes(row)" :key="scope">{{
            scope === '*' ? '全部权限' : baseScopeLabels[scope] ?? scopes.find(item => item.value === scope)?.label ?? scope
          }}</el-tag></template
        ></el-table-column
      >
      <el-table-column label="状态" width="90"
        ><template #default="{ row }"
          ><el-tag :type="row.enabled ? 'success' : 'info'">{{
            row.enabled ? "启用" : "停用"
          }}</el-tag></template
        ></el-table-column
      >
      <el-table-column label="操作" width="140" fixed="right"
        ><template #default="{ row }"
          ><el-button
            text
            :icon="Edit3"
            :disabled="row.builtin || saving"
            aria-label="编辑用户"
            @click="openEdit(row)" /><el-button
            text
            :icon="KeyRound"
            :disabled="row.builtin || saving"
            aria-label="重置密码"
            @click="openReset(row)" /><el-button
            text
            type="danger"
            :icon="Trash2"
            :disabled="row.builtin || !row.enabled || saving"
            aria-label="删除用户"
            @click="openDelete(row)" /></template
      ></el-table-column>
    </el-table>
    <el-pagination v-model:current-page="pageNumber" :page-size="20" :total="total"
      layout="total, prev, pager, next" @current-change="load" />
    <el-dialog
      v-model="open"
      :title="editing ? '编辑用户' : '新建用户'"
      width="min(560px, 94vw)"
      :close-on-click-modal="!saving"
      @closed="closeEditor"
    >
      <el-form label-position="top">
        <el-form-item label="用户名" required
          ><el-input
            v-model="form.username"
            :disabled="Boolean(editing) || saving"
        /></el-form-item>
        <el-form-item label="显示名称" required
          ><el-input v-model="form.displayName" :disabled="saving"
        /></el-form-item>
        <el-form-item v-if="!editing" label="初始密码" required
          ><el-input
            v-model="form.password"
            type="password"
            show-password
            :disabled="saving"
        /></el-form-item>
        <el-form-item label="固定基础权限"
          ><el-tag v-for="scope in baseScopes" :key="scope" type="info" effect="plain">{{ baseScopeLabels[scope] }}</el-tag
          ><p class="access-muted">所有有效用户均拥有基础查看、下载和模板权限，不能移除。</p>
        </el-form-item>
        <el-form-item label="额外功能权限"
          ><el-select
            v-model="form.scopes"
            multiple
            filterable
            :disabled="saving"
            style="width: 100%"
            ><el-option
              v-for="scope in scopes"
              :key="scope.value"
              :label="scope.label"
              :value="scope.value" /></el-select
        ></el-form-item>
        <el-form-item v-if="editing" label="启用"
          ><el-switch
            v-model="form.enabled"
            :disabled="editing.builtin || saving"
        /></el-form-item>
      </el-form>
      <template #footer
        ><el-button :disabled="saving" @click="closeEditor">取消</el-button
        ><el-button type="primary" :loading="saving" @click="save"
          >保存</el-button
        ></template
      >
    </el-dialog>
    <el-dialog
      v-model="resetOpen"
      title="重置密码"
      width="min(420px, 94vw)"
      :close-on-click-modal="!resetting"
      @closed="closeReset"
      ><el-input
        v-model="resetPassword"
        type="password"
        show-password
        aria-label="新密码"
        :disabled="resetting"
      /><template #footer
        ><el-button :disabled="resetting" @click="closeReset">取消</el-button
        ><el-button type="primary" :loading="resetting" @click="reset(editing!)"
          >重置</el-button
        ></template
      ></el-dialog
    >
    <el-dialog
      v-model="deleteOpen"
      title="删除用户"
      width="min(500px, 94vw)"
      :close-on-click-modal="!deleting"
      @closed="closeDelete"
    >
      <p>删除“{{ deletingUser?.displayName }}”后，其登录和服务令牌会立即失效。</p>
      <el-form label-position="top">
        <el-form-item label="该用户创建的设备资源">
          <el-radio-group v-model="resourceAction" :disabled="deleting">
            <el-radio value="DELETE">删除资源并停止关联采集任务</el-radio>
            <el-radio value="TRANSFER">转移资源和本人创建的关联任务</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="resourceAction === 'TRANSFER'" label="资源接收用户" required>
          <el-select v-model="targetUserId" filterable :disabled="deleting" placeholder="请选择有效用户" style="width: 100%">
            <el-option v-for="target in eligibleTransferTargets" :key="target.id" :value="target.id"
              :label="`${target.displayName}（${target.username}）`" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button :disabled="deleting" @click="closeDelete">取消</el-button>
        <el-button type="danger" :loading="deleting" @click="disable">删除用户</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<style scoped>
.user-manager {
  min-width: 0;
}
.access-toolbar,
.toolbar-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.access-toolbar {
  justify-content: space-between;
  margin-bottom: 20px;
}
.access-toolbar h2 {
  margin: 0;
}
.access-id {
  display: block;
  color: #829095;
  font-size: 12px;
}
.el-tag + .el-tag {
  margin-left: 4px;
}
.load-error {
  margin-bottom: 16px;
}
.resource-select {
  width: 100%;
  margin-top: 8px;
}
@media (max-width: 540px) {
  .access-toolbar {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
