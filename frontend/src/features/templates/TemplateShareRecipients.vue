<script setup lang="ts">
// 模板共享收件人只读取最小用户目录；全局共享由管理员显式开启，普通用户只能指定收件人。
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { usersApi, type SessionUser } from "../../shared/api";

const sharedWith = defineModel<string[]>("sharedWith", { required: true });
const sharedWithAll = defineModel<boolean>("sharedWithAll", { required: true });
const props = defineProps<{ isAdmin?: boolean; disabled?: boolean }>();
const items = ref<Pick<SessionUser, "id" | "username" | "displayName">[]>([]);
const loading = ref(false);
const loadError = ref("");
let generation = 0;

const selectedIds = computed({
  get: () => sharedWith.value,
  set: value => { sharedWith.value = [...new Set(value)]; },
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
      loadError.value = error instanceof Error ? error.message : "读取共享收件人失败";
  } finally {
    if (current === generation) loading.value = false;
  }
}

watch(() => props.isAdmin, isAdmin => {
  if (!isAdmin) sharedWithAll.value = false;
}, { immediate: true });
onMounted(() => void loadTargets());
onBeforeUnmount(() => { generation += 1; });
</script>

<template>
  <div class="template-share-recipients">
    <el-select v-model="selectedIds" multiple filterable clearable :loading="loading" :disabled="disabled || sharedWithAll" placeholder="选择可使用此模板的用户" style="width: 100%">
      <el-option v-for="user in items" :key="user.id" :label="`${user.displayName} · ${user.username}`" :value="user.id" />
    </el-select>
    <p v-if="loadError" class="share-error">{{ loadError }} <el-button text type="primary" :disabled="loading" @click="loadTargets">重试</el-button></p>
    <el-checkbox v-if="isAdmin" v-model="sharedWithAll" :disabled="disabled">共享给全部用户</el-checkbox>
    <p class="share-note">共享用户可在创建任务时复制模板配置，不能编辑或删除模板。</p>
  </div>
</template>

<style scoped>
.template-share-recipients { display: grid; gap: 9px; }
.template-share-recipients .el-checkbox { width: fit-content; margin: 0; color: #526568; }
.share-note, .share-error { margin: 0; color: #7b898c; font-size: 12px; line-height: 1.5; }
.share-error { color: #a4484a; }
</style>
