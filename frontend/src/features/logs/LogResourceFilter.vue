<script setup lang="ts">
// 资源筛选按服务端分页加载，包含软删除资源以便检索其历史任务日志。
import { computed, onBeforeUnmount, ref } from "vue";
import { request } from "../../shared/api";
import type { Page, Resource } from "../../shared/types";
const selected = defineModel<string>({ default: "" });
const resources = ref<Resource[]>([]);
const retained = ref<Resource>();
const page = ref(1);
const total = ref(0);
const search = ref("");
const busy = ref(false);
const error = ref("");
let generation = 0;
const choices = computed(() => retained.value && !resources.value.some(item => item.id === retained.value?.id)
  ? [retained.value, ...resources.value] : resources.value);
async function load(value = search.value, nextPage = 1) {
  const current = ++generation;
  search.value = value;
  page.value = nextPage;
  busy.value = true;
  error.value = "";
  try {
    const query = new URLSearchParams({ search: value, page: String(nextPage), pageSize: "20", taskLimit: "1", includeDeleted: "true" });
    const result = await request<Page<Resource>>(`/resources?${query}`);
    if (current === generation) { resources.value = result.items; total.value = result.total; }
  } catch (reason) {
    if (current === generation) error.value = reason instanceof Error ? reason.message : "资源读取失败";
  } finally { if (current === generation) busy.value = false; }
}
function retain(value: string) { retained.value = choices.value.find(item => item.id === value); }
function opened(value: boolean) { if (value) void load(); }
function changePage(value: number) { void load(search.value, value); }
onBeforeUnmount(() => { generation++; });
</script>
<template>
  <div class="resource-filter">
    <el-select v-model="selected" clearable filterable remote :remote-method="load" :loading="busy"
      placeholder="全部设备资源" aria-label="按设备资源筛选日志任务" @visible-change="opened" @change="retain">
      <el-option v-for="item in choices" :key="item.id" :value="item.id"
        :label="`${item.name} · ${item.ip}${item.deletedAt ? '（已删除）' : ''}`" />
      <template #footer>
        <el-pagination small layout="prev, next" :current-page="page" :page-size="20" :total="total"
          @current-change="changePage" />
      </template>
    </el-select>
    <span v-if="error" role="alert">{{ error }}</span>
  </div>
</template>
<style scoped>
.resource-filter { min-width: 0; }
.resource-filter > span { display: block; color: #b33d3d; font-size: 12px; overflow-wrap: anywhere; }
</style>
