<script setup lang="ts">
// 资源共享仅保存指定用户 ID；候选目录加载失败时保留原选择，防止编辑资源意外撤销共享。
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { usersApi, type SessionUser } from "../../shared/api";

const sharedUserIds = defineModel<string[]>({ required: true });
const props = defineProps<{ disabled?: boolean }>();
const items = ref<Pick<SessionUser, "id" | "username" | "displayName">[]>([]);
const loading = ref(false);
const loadError = ref("");
let generation = 0;

const selectedIds = computed({
  get: () => sharedUserIds.value,
  set: value => { sharedUserIds.value = [...new Set(value)]; },
});

async function loadTargets() {
  const current = ++generation;
  loading.value = true;
  loadError.value = "";
  try {
    const collected: typeof items.value = [];
    let page = 1;
    let total = 0;
    do {
      const result = await usersApi.shareTargets(page, 100);
      if (current !== generation) return;
      const before = collected.length;
      collected.push(...result.items);
      total = result.total;
      if (!result.items.length || collected.length === before) break;
      page += 1;
    } while (collected.length < total);
    if (current === generation) items.value = collected;
  } catch (error) {
    if (current === generation)
      loadError.value = error instanceof Error ? error.message : "读取可共享用户失败";
  } finally {
    if (current === generation) loading.value = false;
  }
}

onMounted(() => void loadTargets());
onBeforeUnmount(() => { generation += 1; });
</script>

<template>
  <div class="resource-share-recipients">
    <el-select v-model="selectedIds" multiple filterable clearable :loading="loading" :disabled="props.disabled" placeholder="默认不共享；可选择一个或多个用户" style="width: 100%">
      <el-option v-for="user in items" :key="user.id" :label="`${user.displayName} · ${user.username}`" :value="user.id" />
    </el-select>
    <p v-if="loadError" class="share-error">{{ loadError }} <el-button text type="primary" :disabled="loading || props.disabled" @click="loadTargets">重试</el-button></p>
    <p class="share-note">未选择用户时资源默认不共享。共享用户仍需具备对应平台权限，可编辑和认证资源，不能删除资源。</p>
  </div>
</template>

<style scoped>
.resource-share-recipients { display: grid; gap: 8px; }
.share-note, .share-error { margin: 0; color: #7b898c; font-size: 12px; line-height: 1.5; }
.share-error { color: #a4484a; }
</style>
