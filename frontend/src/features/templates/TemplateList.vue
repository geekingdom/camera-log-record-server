<script setup lang="ts">
// 模板列表区分创建者与共享只读模板；批量删除逐项串行并保留失败结果。
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { Edit3, Trash2 } from "lucide-vue-next";
import { ElMessage } from "element-plus";
import { api } from "../../shared/api";
import { runSequentially, type BulkOperationEntry } from "../../shared/bulkOperations";
import { confirmAction } from "../../shared/confirm";
import type { Template } from "../../shared/types";
import { canManageOwnedRecord } from "../../shared/ownership";
import CreatorFilter from "../../shared/CreatorFilter.vue";
const props = defineProps<{ items: Template[]; canWrite?: boolean; userId?: string; isAdmin?: boolean; createdBy?: string; showAll?: boolean; includeDeleted?: boolean; selectionKey?: string }>();
const emit = defineEmits<{ edit: [Template]; changed: []; filters: [filters: { createdBy: string; showAll: boolean; includeDeleted: boolean }] }>();
const canManage = (template: Template) => Boolean(!template.deletedAt && props.canWrite && canManageOwnedRecord(props.userId, props.isAdmin, template.createdBy));
const selected = ref<Template[]>([]);
const deleting = ref(false);
const results = ref<BulkOperationEntry[]>([]);
const table = ref<{ clearSelection: () => void; toggleRowSelection: (row: Template, selected?: boolean) => void }>();
const failureResults = computed(() => results.value.filter((item) => item.status === "error"));
const skippedResults = computed(() => results.value.filter((item) => item.status === "skipped"));
let pageGeneration = 0;
let mounted = true;

function updateSelection(items: Template[]) {
  selected.value = items.filter(canManage);
}

function clearSelection() {
  selected.value = [];
  table.value?.clearSelection();
}

function applyFilters(change: Partial<{ createdBy: string; showAll: boolean; includeDeleted: boolean }>) {
  emit("filters", { createdBy: props.createdBy || "", showAll: Boolean(props.showAll), includeDeleted: Boolean(props.includeDeleted), ...change });
}
function applyCreatorFilter(value: string) {
  applyFilters({ createdBy: value });
}
function applyShowAll(value: unknown) {
  applyFilters({ showAll: Boolean(value) });
}
function applyIncludeDeleted(value: unknown) {
  applyFilters({ includeDeleted: Boolean(value) });
}

const pageIds = computed(() => props.items.map((item) => item.id).join("\u0000"));

// 仅页码或查询导致的 ID 集合变化才清除选择；同页轮询要保留选择但按新快照复核所有权。
watch(pageIds, () => {
  if (deleting.value) return;
  pageGeneration += 1;
  clearSelection();
});
watch(() => props.selectionKey, () => {
  pageGeneration += 1;
  clearSelection();
});
watch(() => props.items, async (items) => {
  const selectedIds = new Set(selected.value.map((item) => item.id));
  const current = items.filter((item) => selectedIds.has(item.id) && canManage(item));
  const sameSelection = current.length === selected.value.length
    && current.every((item) => selectedIds.has(item.id));
  selected.value = current;
  if (sameSelection) return;
  await nextTick();
  table.value?.clearSelection();
  current.forEach((item) => table.value?.toggleRowSelection(item, true));
});
onBeforeUnmount(() => { mounted = false; });

async function remove(template: Template) {
  if (!canManage(template)) return;
  if (!await confirmAction(`删除模板“${template.name}”？`, "删除模板")) return;
  try {
    await api.deleteTemplate(template.id, template.version ?? 1);
    emit("changed");
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "删除失败");
  }
}

async function removeSelected() {
  if (deleting.value || !selected.value.length) return;
  const current = [...selected.value];
  const generation = pageGeneration;
  if (!await confirmAction(`确认删除 ${current.length} 个模板：${current.map((item) => `“${item.name}”`).join("、")}？`, "确认批量删除模板")) return;
  if (!mounted || pageGeneration !== generation) {
    clearSelection();
    return;
  }
  deleting.value = true;
  results.value = [];
  results.value = await runSequentially(current, (id, version) => api.deleteTemplate(id, version), {
    shouldContinue: () => mounted && pageGeneration === generation,
    onProgress: (entries) => { results.value = entries; },
  });
  deleting.value = false;
  clearSelection();
  const succeeded = results.value.filter((item) => item.status === "success").length;
  if (succeeded) emit("changed");
  if (failureResults.value.length)
    ElMessage.warning(`已删除 ${succeeded} 个模板；${failureResults.value.length} 个失败`);
  else if (skippedResults.value.length)
    ElMessage.warning(`已删除 ${succeeded} 个模板；${skippedResults.value.length} 个未执行`);
  else ElMessage.success(`已删除 ${succeeded} 个模板`);
}
</script>
<template>
  <div class="table-actions">
    <CreatorFilter v-if="props.showAll" :model-value="props.createdBy || ''" @change="applyCreatorFilter" />
    <el-checkbox :model-value="Boolean(props.showAll)" @change="applyShowAll">查看全部</el-checkbox>
    <el-checkbox :model-value="Boolean(props.includeDeleted)" @change="applyIncludeDeleted">包含已删除</el-checkbox>
    <el-button type="danger" :icon="Trash2" :disabled="!selected.length || deleting" :loading="deleting" @click="removeSelected"
      >删除已选 {{ selected.length ? `(${selected.length})` : "" }}</el-button>
  </div>
  <p v-if="results.length" class="bulk-delete-result" aria-live="polite">
    <template v-if="failureResults.length">
      删除失败：{{ failureResults.map((item) => `“${item.name}”${item.error ? `（${item.error}）` : ""}`).join("、") }}
    </template>
    <template v-else-if="skippedResults.length">
      未执行：{{ skippedResults.map((item) => `“${item.name}”`).join("、") }}
    </template>
    <template v-else>已删除 {{ results.length }} 个模板</template>
  </p>
  <el-table
    ref="table"
    scrollbar-always-on
    :data="props.items"
    row-key="id"
    class="data-table"
    empty-text="暂无模板"
    @selection-change="updateSelection"
  >
    <el-table-column type="selection" width="48" reserve-selection :selectable="canManage" />
    <el-table-column
      prop="name"
      label="名称"
      min-width="180" /><el-table-column
      prop="description"
      label="说明"
      min-width="220" /><el-table-column label="创建用户" min-width="130"><template #default="{ row }">{{ row.createdByName || row.createdBy || "历史模板" }}</template></el-table-column><el-table-column label="共享范围" min-width="130"><template #default="{ row }"><el-tag v-if="row.sharedWithAll" type="success" effect="plain">全部用户</el-tag><span v-else-if="row.sharedWith?.length">已共享 {{ row.sharedWith.length }} 人</span><span v-else>仅创建者</span></template></el-table-column><el-table-column
      prop="version"
      label="版本"
      width="100" /><el-table-column label="创建时间" min-width="180"><template #default="{ row }">{{ row.createdAt ? new Date(row.createdAt).toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' }) : '-' }}</template></el-table-column><el-table-column label="删除时间" min-width="180"><template #default="{ row }">{{ row.deletedAt ? new Date(row.deletedAt).toLocaleString('zh-CN', { hour12: false }) : '-' }}</template></el-table-column><el-table-column label="操作" width="120"
      ><template #default="{ row }"
        ><el-button v-if="canManage(row)" text :icon="Edit3" aria-label="编辑模板" @click="emit('edit', row)" /><el-button v-if="canManage(row)"
          text
          type="danger"
          :icon="Trash2"
          aria-label="删除模板"
          @click="remove(row)" /></template></el-table-column
  ></el-table>
 </template>
