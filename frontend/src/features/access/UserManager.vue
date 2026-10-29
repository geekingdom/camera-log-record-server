<script setup lang="ts">
// 用户账号管理：固定基础权限由目录标识，额外权限与管理员角色可按上海自然日到期收回。
import { computed, onMounted, ref } from "vue";
import { Edit3, KeyRound, Plus, RefreshCw, Trash2 } from "lucide-vue-next";
import { ElMessage } from "element-plus";
import { ApiError, usersApi, type SessionUser, type UserRole } from "../../shared/api";
import { userSaveFailureMessage } from "./userManagerMessages";
import {
  canManageUser,
  formPermissionScopes,
  permissionGroups,
  roleLabel,
  type PermissionDirectoryItem,
} from "./userManagerPermissions";

const props = defineProps<{ currentUser?: SessionUser }>();
const users = ref<SessionUser[]>([]);
const scopes = ref<PermissionDirectoryItem[]>([]);
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
  role: "USER" as Exclude<UserRole, "SUPER_ADMIN">,
  permissionsExpiresOn: null as string | null,
  enabled: true,
});
const expirationMode = ref<"permanent" | "date">("permanent");
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
    scopes.value = permissions.scopes;
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
    role: "USER",
    permissionsExpiresOn: null,
    enabled: true,
  };
  expirationMode.value = "permanent";
}
function openCreate() {
  editing.value = undefined;
  resetForm();
  open.value = true;
}
function openEdit(user: SessionUser) {
  if (!canManage(user)) return;
  editing.value = user;
  form.value = {
    username: user.username,
    displayName: user.displayName,
    password: "",
    scopes: formPermissionScopes(user.scopes, scopes.value),
    role: user.role === "ADMIN" ? "ADMIN" : "USER",
    permissionsExpiresOn: user.permissionsExpiresOn ?? null,
    enabled: user.enabled,
  };
  expirationMode.value = user.permissionsExpiresOn ? "date" : "permanent";
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
  if (!canManage(user)) return;
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
const currentEffectiveRole = computed<UserRole | undefined>(() => props.currentUser?.effectiveRole ?? props.currentUser?.role);
const fixedScopes = computed(() => permissionGroups(scopes.value).fixed);
const additionalScopes = computed(() => permissionGroups(scopes.value).additional);
function displayScopes(user: SessionUser) {
  if (user.effectiveRole === "SUPER_ADMIN" || user.effectiveRole === "ADMIN" || (!user.effectiveRole && user.isAdmin)) return ["*"];
  const additional = user.permissionsExpired ? [] : formPermissionScopes(user.scopes, scopes.value);
  return [...fixedScopes.value.map((scope) => scope.value), ...additional];
}
function scopeLabel(scope: string) {
  return scope === "*" ? "全部权限" : scopes.value.find((item) => item.value === scope)?.label ?? scope;
}
function canManage(user: SessionUser) {
  return canManageUser(props.currentUser?.id, currentEffectiveRole.value, user);
}
function expirationLabel(user: SessionUser) {
  if (user.permissionsExpired) return "已到期";
  return user.permissionsExpiresOn ?? "长期";
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
    const permissionsExpiresOn = expirationMode.value === "date" ? form.value.permissionsExpiresOn : null;
    if (expirationMode.value === "date" && !permissionsExpiresOn)
      return ElMessage.warning("请选择额外权限截止日期");
    if (editing.value)
      await usersApi.update(editing.value.id, {
        displayName: form.value.displayName.trim(),
        scopes: form.value.scopes,
        role: form.value.role,
        permissionsExpiresOn,
        enabled: form.value.enabled,
        version: editing.value.version ?? 1,
      });
    else
      await usersApi.create({
        username: form.value.username.trim(),
        displayName: form.value.displayName.trim(),
        password: form.value.password,
        role: form.value.role,
        scopes: form.value.scopes,
        permissionsExpiresOn,
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
  if (!user || !canManage(user)) return;
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
  if (!canManage(user)) return;
  editing.value = user;
  resetPassword.value = "";
  resetOpen.value = true;
}
// 密码重置后立即清空值，关闭弹窗同样清空，避免密码残留在组件状态中。
async function reset(user: SessionUser) {
  if (!canManage(user)) return;
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
      <el-table-column label="角色" width="120"
        ><template #default="{ row }"
          ><el-tag :type="row.effectiveRole === 'SUPER_ADMIN' ? 'danger' : row.effectiveRole === 'ADMIN' || (!row.effectiveRole && row.isAdmin) ? 'warning' : 'info'">{{ roleLabel(row.role) }}</el-tag
          ><span v-if="row.effectiveRole && row.effectiveRole !== row.role" class="access-id">当前：{{ roleLabel(row.effectiveRole) }}</span></template
        ></el-table-column
      >
      <el-table-column label="额外权限有效期" width="132"
        ><template #default="{ row }"
          ><el-tag :type="row.permissionsExpired ? 'warning' : 'info'">{{ expirationLabel(row) }}</el-tag></template
        ></el-table-column
      >
      <el-table-column label="权限" min-width="210"
        ><template #default="{ row }"
          ><el-tag v-for="scope in displayScopes(row)" :key="scope">{{
            scopeLabel(scope)
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
            :disabled="!canManage(row) || saving"
            aria-label="编辑用户"
            @click="openEdit(row)" /><el-button
            text
            :icon="KeyRound"
            :disabled="!canManage(row) || saving"
            aria-label="重置密码"
            @click="openReset(row)" /><el-button
            text
            type="danger"
            :icon="Trash2"
            :disabled="!canManage(row) || !row.enabled || saving"
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
        <el-form-item label="角色" required>
          <el-radio-group v-model="form.role" :disabled="saving">
            <el-radio value="USER">普通用户</el-radio>
            <el-radio value="ADMIN">管理员</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="固定基础权限"
          ><el-tag v-for="scope in fixedScopes" :key="scope.value" type="info" effect="plain">{{ scope.label }}</el-tag
          ><p class="access-muted">固定基础权限由服务端统一配置，不能在此移除。</p>
        </el-form-item>
        <el-form-item label="额外功能权限"
          ><el-select
            v-model="form.scopes"
            multiple
            filterable
            :disabled="saving"
            style="width: 100%"
            ><el-option
              v-for="scope in additionalScopes"
              :key="scope.value"
              :label="scope.label"
              :value="scope.value" /></el-select
        ></el-form-item>
        <el-form-item label="额外权限有效期">
          <el-radio-group v-model="expirationMode" :disabled="saving">
            <el-radio value="permanent">长期</el-radio>
            <el-radio value="date">指定截止日</el-radio>
          </el-radio-group>
          <el-date-picker
            v-if="expirationMode === 'date'"
            v-model="form.permissionsExpiresOn"
            type="date"
            value-format="YYYY-MM-DD"
            format="YYYY年MM月DD日"
            :disabled="saving"
            :disabled-date="(date: Date) => date.getTime() < new Date(new Date().toDateString()).getTime()"
            placeholder="选择截止日期"
            style="width: 100%; margin-top: 8px"
          />
          <p class="access-muted">截止日按北京时间当天结束计算；到期后仅保留固定基础权限。</p>
        </el-form-item>
        <el-form-item v-if="editing" label="启用"
          ><el-switch
            v-model="form.enabled"
            :disabled="!canManage(editing) || saving"
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
