<script setup lang="ts">
// 业务模块按首次访问加载；分块加载失败后整页刷新，不会重放已挂载页面中的业务请求。
import { onBeforeUnmount, onMounted, ref, shallowRef, watch } from "vue";
import type { Component } from "vue";

const props = defineProps<{
  loader: () => Promise<{ default: Component }>;
  componentProps?: Record<string, unknown>;
  listeners?: Record<string, (...args: any[]) => void>;
  overlay?: boolean;
}>();

const view = shallowRef<Component>();
const componentRef = ref<{ reload?: () => unknown }>();
const loading = ref(false);
const failed = ref(false);
let generation = 0;
let mounted = true;

async function load() {
  const current = ++generation;
  loading.value = true;
  failed.value = false;
  view.value = undefined;
  try {
    const loaded = await props.loader();
    if (mounted && current === generation) view.value = loaded.default;
  } catch {
    if (mounted && current === generation) failed.value = true;
  } finally {
    if (mounted && current === generation) loading.value = false;
  }
}

watch(() => props.loader, () => void load());
onMounted(() => void load());
onBeforeUnmount(() => { mounted = false; generation++; });

async function reload() {
  if (loading.value || failed.value) return;
  if (view.value && componentRef.value?.reload) await componentRef.value.reload();
  else if (!view.value) await load();
}

function reloadPage() {
  window.location.reload();
}

defineExpose({ reload, retry: reloadPage });
</script>

<template>
  <div class="async-view" :class="{ 'async-view-overlay': overlay && (loading || failed) }">
    <section v-if="loading" class="async-view-state" data-testid="async-view-loading" aria-live="polite">
      <el-skeleton :rows="5" animated />
    </section>
    <section v-else-if="failed" class="async-view-state async-view-error" data-testid="async-view-error" role="alert">
      <el-alert title="页面加载失败" type="error" :closable="false" show-icon />
      <el-button data-testid="async-view-retry" @click="reloadPage">重新加载</el-button>
    </section>
    <component ref="componentRef" :is="view" v-else-if="view" v-bind="componentProps" v-on="listeners" />
  </div>
</template>
