<script setup lang="ts">
// 管理员维护全局常用命令；普通用户只能读取启用项，无法从此页面修改共享配置。
import { onMounted, ref } from "vue";
import { Edit3, Plus, Power, RefreshCw, Trash2 } from "lucide-vue-next";
import { ElMessage } from "element-plus";
import { confirmAction } from "../../shared/confirm";
import { settingsApi, type CommonCommand } from "./api";

const rows = ref<CommonCommand[]>([]);
const loading = ref(false);
const page = ref(1);
const total = ref(0);
const pageSize = 100;
const saving = ref(false);
const deleting = ref("");
const dialogOpen = ref(false);
const editing = ref<CommonCommand>();
const form = ref({ command: "", description: "", enabled: true });
let loadGeneration = 0;
let editingSession = 0;

// 翻页与刷新可并发到达；只让最新请求写入对应页，避免序号和数据来自不同页。
async function load(requestedPage = page.value) {
  const current = ++loadGeneration;
  loading.value = true;
  try {
    const result = await settingsApi.adminCommonCommands(requestedPage, pageSize);
    if (current !== loadGeneration) return;
    if (!result.items.length && result.total && requestedPage > 1) {
      page.value = requestedPage - 1;
      await load(page.value);
      return;
    }
    page.value = requestedPage;
    rows.value = result.items;
    total.value = result.total;
  }
  catch (error) {
    if (current === loadGeneration) ElMessage.error(error instanceof Error ? error.message : "读取常用命令失败");
  }
  finally { if (current === loadGeneration) loading.value = false; }
}
function openCreate() {
  editingSession += 1;
  editing.value = undefined;
  form.value = { command: "", description: "", enabled: true };
  dialogOpen.value = true;
}
function openEdit(row: CommonCommand) {
  editingSession += 1;
  editing.value = row;
  form.value = { command: row.command, description: row.description, enabled: row.enabled };
  dialogOpen.value = true;
}
async function save() {
  const editTarget = editing.value;
  const session = editingSession;
  const command = form.value.command.trim();
  const description = form.value.description.trim();
  const enabled = form.value.enabled;
  if (!command) return ElMessage.warning("请输入命令");
  if (saving.value) return;
  saving.value = true;
  try {
    if (editTarget) await settingsApi.updateCommonCommand(editTarget.id, { version: editTarget.version, command, description, enabled });
    else await settingsApi.createCommonCommand({ command, description, enabled });
    if (session === editingSession) {
      dialogOpen.value = false;
      ElMessage.success(editTarget ? "常用命令已更新" : "常用命令已新增");
    }
    page.value = 1;
    await load(1);
  } catch (error) {
    if (session === editingSession) ElMessage.error(error instanceof Error ? error.message : "保存常用命令失败");
  }
  finally { saving.value = false; }
}
async function toggle(row: CommonCommand) {
  try {
    await settingsApi.updateCommonCommand(row.id, { version: row.version, enabled: !row.enabled });
    ElMessage.success(row.enabled ? "常用命令已禁用" : "常用命令已启用");
    await load();
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : "更新命令状态失败"); }
}
async function remove(row: CommonCommand) {
  if (deleting.value || !await confirmAction(`确认删除常用命令“${row.command}”吗？`, "确认删除常用命令")) return;
  deleting.value = row.id;
  try { await settingsApi.deleteCommonCommand(row.id, row.version); ElMessage.success("常用命令已删除"); await load(); }
  catch (error) { ElMessage.error(error instanceof Error ? error.message : "删除常用命令失败"); }
  finally { deleting.value = ""; }
}
onMounted(() => void load());
</script>

<template>
  <section class="common-commands" aria-label="常用命令配置">
    <div class="common-commands-heading"><div><h3>常用命令</h3><p>管理员维护的启用命令会在所有用户的日志工作台中提供快捷补全。</p></div><div><el-tooltip content="刷新常用命令"><el-button :icon="RefreshCw" aria-label="刷新常用命令" @click="load()" /></el-tooltip><el-button type="primary" :icon="Plus" @click="openCreate">新增命令</el-button></div></div>
    <div class="common-commands-table"><el-table v-loading="loading" :data="rows" empty-text="暂无常用命令"><el-table-column label="序号" width="72"><template #default="{ $index }">{{ (page - 1) * pageSize + $index + 1 }}</template></el-table-column><el-table-column prop="command" label="命令" min-width="260" show-overflow-tooltip /><el-table-column prop="description" label="描述" min-width="220" show-overflow-tooltip /><el-table-column label="状态" width="100"><template #default="{ row }"><el-tag :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '禁用' }}</el-tag></template></el-table-column><el-table-column label="操作" width="190" fixed="right"><template #default="{ row }"><el-button text :icon="Edit3" :aria-label="`编辑常用命令 ${row.command}`" @click="openEdit(row)">编辑</el-button><el-button text :icon="Power" :aria-label="`${row.enabled ? '禁用' : '启用'}常用命令 ${row.command}`" @click="toggle(row)">{{ row.enabled ? '禁用' : '启用' }}</el-button><el-button text type="danger" :icon="Trash2" :loading="deleting === row.id" :disabled="Boolean(deleting)" :aria-label="`删除常用命令 ${row.command}`" @click="remove(row)">删除</el-button></template></el-table-column></el-table></div><el-pagination v-if="total > pageSize" v-model:current-page="page" background layout="total, prev, pager, next" :page-size="pageSize" :total="total" @current-change="load" />
    <el-dialog v-model="dialogOpen" :title="editing ? '编辑常用命令' : '新增常用命令'" width="min(560px, 94vw)" destroy-on-close><el-form label-position="top"><el-form-item label="命令" required><el-input v-model="form.command" maxlength="8192" aria-label="常用命令" /></el-form-item><el-form-item label="描述"><el-input v-model="form.description" maxlength="512" show-word-limit aria-label="常用命令描述" /></el-form-item><el-form-item label="启用"><el-switch v-model="form.enabled" aria-label="启用常用命令" /></el-form-item></el-form><template #footer><el-button @click="dialogOpen = false">取消</el-button><el-button type="primary" :loading="saving" @click="save">保存</el-button></template></el-dialog>
  </section>
</template>

<style scoped>
.common-commands { min-width: 0; }.common-commands-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 16px; }.common-commands-heading h3 { margin: 0; font-size: 16px; }.common-commands-heading p { margin: 5px 0 0; color: var(--el-text-color-secondary); font-size: 13px; line-height: 1.5; }.common-commands-heading > div:last-child { display: flex; gap: 8px; }.common-commands-table { overflow-x: auto; border: 1px solid var(--el-border-color-lighter); border-radius: 6px; }.common-commands-table :deep(.el-table) { min-width: 760px; }.common-commands > .el-pagination { margin-top: 12px; justify-content: flex-end; } @media (max-width: 620px) { .common-commands-heading { flex-direction: column; }.common-commands-heading > div:last-child { width: 100%; }.common-commands-heading > div:last-child .el-button:last-child { flex: 1; } }
</style>
