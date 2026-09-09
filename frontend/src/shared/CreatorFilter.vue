<script setup lang="ts">
// 创建人筛选使用最小分页目录；历史账号仍可筛选，迟到请求不覆盖新搜索。
import { computed, onBeforeUnmount, ref } from "vue";
import { request } from "./api";
import type { Page } from "./types";
interface Creator { id: string; username: string; displayName: string }
const selected = defineModel<string>({ default: "" });
const emit = defineEmits<{ change: [string] }>();
const rows = ref<Creator[]>([]), retained = ref<Creator>();
const page = ref(1), total = ref(0), search = ref("");
const busy = ref(false), error = ref("");
let generation = 0;
const choices = computed(() => retained.value && !rows.value.some(row => row.id === retained.value?.id)
  ? [retained.value, ...rows.value] : rows.value);
async function load(value = search.value, requestedPage = 1) {
  const current = ++generation;
  search.value = value; page.value = requestedPage; busy.value = true; error.value = "";
  try {
    const query = new URLSearchParams({ search: value, page: String(requestedPage), pageSize: "20" });
    const result = await request<Page<Creator>>(`/users/creators?${query}`);
    if (current === generation) { rows.value = result.items; total.value = result.total; }
  } catch (reason) {
    if (current === generation) error.value = reason instanceof Error ? reason.message : "创建用户读取失败";
  } finally { if (current === generation) busy.value = false; }
}
function change(value: string | undefined) {
  retained.value = choices.value.find(row => row.id === value);
  emit("change", value || "");
}
function visibleChange(open: boolean) {
  if (open) void load();
}
function changePage(value: number) {
  void load(search.value, value);
}
onBeforeUnmount(() => { generation++; });
</script>
<template>
  <div class="creator-filter">
    <el-select v-model="selected" clearable filterable remote :remote-method="load" :loading="busy"
      placeholder="全部创建用户" aria-label="按创建用户筛选" @visible-change="visibleChange" @change="change">
      <el-option v-for="item in choices" :key="item.id" :value="item.id" :label="`${item.displayName || item.username} · ${item.username}`" />
      <template #footer><el-pagination small layout="prev, next" :current-page="page" :page-size="20" :total="total"
        @current-change="changePage" /></template>
    </el-select>
    <span v-if="error" role="alert">{{ error }}</span>
  </div>
</template>
<style scoped>
.creator-filter { min-width: 0; width: 220px; max-width: 100%; }
.creator-filter > span { display: block; color: #b33d3d; font-size: 12px; overflow-wrap: anywhere; }
</style>
