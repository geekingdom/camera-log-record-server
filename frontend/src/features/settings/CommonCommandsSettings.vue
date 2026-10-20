<script setup lang="ts">
// 管理员维护全局常用命令；普通用户只能读取启用项，无法从此页面修改共享配置。
import { onMounted, ref } from "vue";
import { Edit3, Plus, Power, RefreshCw, Trash2 } from "lucide-vue-next";
import { ElMessage } from "element-plus";
import { confirmAction } from "../../shared/confirm";
import { idempotencyKey } from "../../shared/api";
import { settingsApi, type CommonCommand, type CommonCommandBatchAction, type CommonCommandBatchItem } from "./api";

const rows = ref<CommonCommand[]>([]);
const loading = ref(false);
const page = ref(1);
const total = ref(0);
const pageSize = ref<20 | 50 | 100>(20);
const query = ref("");
const enabled = ref<"" | "true" | "false">("");
const selected = ref<CommonCommand[]>([]);
const batching = ref(false);
const table = ref<{ clearSelection: () => void }>();
const saving = ref(false);
const deleting = ref("");
const dialogOpen = ref(false);
const editing = ref<CommonCommand>();
const form = ref({ command: "", description: "", enabled: true });
let loadGeneration = 0;
let editingSession = 0;
let selectionGeneration = 0;
let pendingBatch: { action: CommonCommandBatchAction; items: CommonCommandBatchItem[]; key: string } | undefined;

// 翻页与刷新可并发到达；只让最新请求写入对应页，避免序号和数据来自不同页。
async function load(requestedPage = page.value) {
  const current = ++loadGeneration;
  clearSelection();
  loading.value = true;
  try {
    const result = await settingsApi.adminCommonCommands(requestedPage, pageSize.value, {
      query: query.value, enabled: enabled.value === "" ? undefined : enabled.value === "true",
    });
    if (current !== loadGeneration) return;
    if (!result.items.length && result.total && requestedPage > 1) {
      page.value = requestedPage - 1;
      await load(page.value);
      return;
    }
    page.value = result.total ? requestedPage : 1;
    rows.value = result.items;
    total.value = result.total;
  }
  catch (error) {
    if (current === loadGeneration) ElMessage.error(error instanceof Error ? error.message : "读取常用命令失败");
  }
  finally { if (current === loadGeneration) loading.value = false; }
}
function clearSelection() { selectionGeneration += 1; selected.value = []; table.value?.clearSelection(); }
function updateSelection(items: CommonCommand[]) { selectionGeneration += 1; selected.value = items; }
function resetAndLoad() { clearSelection(); page.value = 1; void load(1); }
function changePage(target: number) { clearSelection(); void load(target); }
async function batch(action: CommonCommandBatchAction, target?: CommonCommand[]) {
  const labels = { ENABLE: "启用", DISABLE: "禁用", DELETE: "删除" };
  const source = target ?? selected.value;
  const items = source.map(({ id, version }) => ({ id, version }));
  if (!items.length || batching.value) return;
  const intent = selectionGeneration, request = loadGeneration;
  if (!await confirmAction(`确认${labels[action]}选中的 ${items.length} 条常用命令吗？`, `批量${labels[action]}常用命令`)) return;
  if (intent !== selectionGeneration || request !== loadGeneration) return;
  const samePending = pendingBatch && pendingBatch.action === action && JSON.stringify(pendingBatch.items) === JSON.stringify(items);
  const operation = samePending && pendingBatch ? pendingBatch : { action, items, key: idempotencyKey() };
  pendingBatch = operation;
  batching.value = true;
  try {
    const result = await settingsApi.batchCommonCommands(operation.action, operation.items, operation.key);
    ElMessage.success(`已${labels[action]} ${result.affected} 条常用命令`); pendingBatch = undefined; clearSelection(); await load();
  } catch (error) {
    const message = error instanceof Error ? error.message : "批量操作失败，请使用相同选择重试";
    ElMessage.error(message);
    if (message.includes("版本")) { pendingBatch = undefined; clearSelection(); await load(); }
  } finally { batching.value = false; }
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
    clearSelection(); page.value = 1;
    await load(1);
  } catch (error) {
    if (session === editingSession) ElMessage.error(error instanceof Error ? error.message : "保存常用命令失败");
  }
  finally { saving.value = false; }
}
async function remove(row: CommonCommand) {
  if (deleting.value || !await confirmAction(`确认删除常用命令“${row.command}”吗？`, "确认删除常用命令")) return;
  deleting.value = row.id;
  try { await settingsApi.deleteCommonCommand(row.id, row.version); ElMessage.success("常用命令已删除"); clearSelection(); await load(); }
  catch (error) { ElMessage.error(error instanceof Error ? error.message : "删除常用命令失败"); }
  finally { deleting.value = ""; }
}
onMounted(() => void load());
</script>

<template>
  <section class="common-commands" aria-label="常用命令配置">
    <div class="common-commands-heading"><div><h3>常用命令</h3><p>管理员维护的启用命令会在所有用户的日志工作台中提供快捷补全。</p></div><div><el-tooltip content="刷新常用命令"><el-button :icon="RefreshCw" aria-label="刷新常用命令" @click="load()" /></el-tooltip><el-button type="primary" :icon="Plus" @click="openCreate">新增命令</el-button></div></div>
    <div class="filters"><el-input v-model="query" clearable placeholder="按命令或描述搜索" aria-label="搜索常用命令" @keyup.enter="resetAndLoad" @clear="resetAndLoad" /><el-select v-model="enabled" placeholder="全部状态" aria-label="常用命令状态" @change="resetAndLoad"><el-option label="全部状态" value="" /><el-option label="启用" value="true" /><el-option label="禁用" value="false" /></el-select><el-button @click="resetAndLoad">查询</el-button></div>
    <div class="batch-actions"><span>已选 {{ selected.length }} 条</span><el-button :disabled="!selected.length || batching" @click="batch('ENABLE')">批量启用</el-button><el-button :disabled="!selected.length || batching" @click="batch('DISABLE')">批量禁用</el-button><el-button type="danger" :disabled="!selected.length || batching" @click="batch('DELETE')">批量删除</el-button></div>
    <div class="common-commands-table"><el-table ref="table" v-loading="loading" :data="rows" row-key="id" empty-text="暂无常用命令" @selection-change="updateSelection"><el-table-column type="selection" width="48" /><el-table-column label="序号" width="72"><template #default="{ $index }">{{ (page - 1) * pageSize + $index + 1 }}</template></el-table-column><el-table-column prop="command" label="命令" min-width="260" show-overflow-tooltip /><el-table-column prop="description" label="描述" min-width="220" show-overflow-tooltip /><el-table-column label="状态" width="100"><template #default="{ row }"><el-tag :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '禁用' }}</el-tag></template></el-table-column><el-table-column label="操作" width="190" fixed="right"><template #default="{ row }"><el-button text :icon="Edit3" :aria-label="`编辑常用命令 ${row.command}`" @click="openEdit(row)">编辑</el-button><el-button text :icon="Power" :aria-label="`${row.enabled ? '禁用' : '启用'}常用命令 ${row.command}`" @click="batch(row.enabled ? 'DISABLE' : 'ENABLE', [row])">{{ row.enabled ? '禁用' : '启用' }}</el-button><el-button text type="danger" :icon="Trash2" :loading="deleting === row.id" :disabled="Boolean(deleting)" :aria-label="`删除常用命令 ${row.command}`" @click="remove(row)">删除</el-button></template></el-table-column></el-table></div><el-pagination v-model:current-page="page" v-model:page-size="pageSize" background layout="total, sizes, prev, pager, next" :page-sizes="[20, 50, 100]" :total="total" @current-change="changePage" @size-change="resetAndLoad" />
    <el-dialog v-model="dialogOpen" :title="editing ? '编辑常用命令' : '新增常用命令'" width="min(560px, 94vw)" destroy-on-close><el-form label-position="top"><el-form-item label="命令" required><el-input v-model="form.command" maxlength="8192" aria-label="常用命令" /></el-form-item><el-form-item label="描述"><el-input v-model="form.description" maxlength="512" show-word-limit aria-label="常用命令描述" /></el-form-item><el-form-item label="启用"><el-switch v-model="form.enabled" aria-label="启用常用命令" /></el-form-item></el-form><template #footer><el-button @click="dialogOpen = false">取消</el-button><el-button type="primary" :loading="saving" @click="save">保存</el-button></template></el-dialog>
  </section>
</template>

<style scoped>
.common-commands { min-width: 0; }.common-commands-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 16px; }.common-commands-heading h3 { margin: 0; font-size: 16px; }.common-commands-heading p { margin: 5px 0 0; color: var(--el-text-color-secondary); font-size: 13px; line-height: 1.5; }.common-commands-heading > div:last-child,.filters,.batch-actions { display:flex; gap:8px; align-items:center; }.filters { margin-bottom:12px; }.filters .el-input { max-width:360px; }.filters .el-select { width:120px; }.batch-actions { margin-bottom:10px; color:var(--el-text-color-secondary); font-size:13px; }.common-commands-table { overflow-x: auto; border: 1px solid var(--el-border-color-lighter); border-radius: 6px; }.common-commands-table :deep(.el-table) { min-width:808px; }.common-commands > .el-pagination { margin-top: 12px; justify-content: flex-end; flex-wrap: wrap; gap: 8px; } @media (max-width: 620px) { .common-commands-heading,.filters { flex-direction: column; }.common-commands-heading > div:last-child,.filters .el-input,.filters .el-select { width:100%; max-width:none; }.common-commands-heading > div:last-child .el-button:last-child { flex: 1; }.batch-actions { flex-wrap:wrap; } .common-commands > .el-pagination { justify-content: flex-start; } }
</style>
