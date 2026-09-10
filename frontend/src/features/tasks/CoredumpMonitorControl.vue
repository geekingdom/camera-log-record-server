<script setup lang="ts">
// Coredump 开关展示区分任务配置与资源级实际负责人，避免共享展示写回新任务。
import { computed } from "vue";

import type { CoredumpMonitorStatus } from "../../shared/types";

const props = defineProps<{
  modelValue?: boolean;
  status: CoredumpMonitorStatus | null;
  loading: boolean;
  error: string | null;
  taskId?: string;
}>();
const emit = defineEmits<{ "update:modelValue": [value: boolean] }>();
const sharedOwner = computed(() => props.status?.active ? props.status.ownerTask : null);
const ownedByCurrentTask = computed(() => Boolean(sharedOwner.value && sharedOwner.value.id === props.taskId));
const sharedByAnotherTask = computed(() => Boolean(sharedOwner.value && !ownedByCurrentTask.value));
const ownerDescription = computed(() => sharedOwner.value
  ? `当前由任务“${sharedOwner.value.name}”负责 Coredump NFS 挂载监控`
  : "当前没有正在负责 Coredump NFS 挂载监控的任务");
const mountDescription = computed(() => ({
  MOUNTED: "已挂载",
  FAILED: "挂载失败",
  PENDING: "待确认",
  UNMOUNTED: "已卸载",
  UNMOUNT_SKIPPED: "跳过卸载，结果未确认",
  UNMOUNT_FAILED: "卸载失败",
}[props.status?.mountStatus ?? ""] ?? props.status?.mountStatus));
</script>

<template>
  <el-form-item label="Coredump 监控">
    <div class="coredump-monitor-control">
      <el-alert
        v-if="error"
        title="无法确认共享 Coredump 监控状态"
        :description="error"
        type="warning"
        :closable="false"
        show-icon
      />
      <template v-else-if="sharedByAnotherTask">
        <el-switch :model-value="true" disabled active-text="已由同资源任务启用 Coredump 监控" />
        <small class="inline-option">{{ ownerDescription }}<template v-if="mountDescription">；挂载状态：{{ mountDescription }}</template></small>
      </template>
      <template v-else>
        <el-switch
          :model-value="modelValue"
          :loading="loading"
          active-text="启用 Coredump NFS 挂载监控"
          @update:model-value="emit('update:modelValue', $event)"
        />
        <small v-if="ownedByCurrentTask" class="inline-option">{{ ownerDescription }}<template v-if="mountDescription">；挂载状态：{{ mountDescription }}</template></small>
        <small v-else-if="!loading" class="inline-option">{{ ownerDescription }}</small>
      </template>
    </div>
  </el-form-item>
</template>

<style scoped>
.coredump-monitor-control {
  align-items: flex-start;
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.coredump-monitor-control .inline-option {
  line-height: 1.5;
  overflow-wrap: anywhere;
}
</style>
