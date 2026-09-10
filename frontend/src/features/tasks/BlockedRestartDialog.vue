<script setup lang="ts">
// 阻塞任务重启必须先获得操作者确认；仅收到隔离要求后才向管理员显示证据输入。
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { ApiError, api } from "../../shared/api";
import type { Task } from "../../shared/types";

const task = defineModel<Task | undefined>({ required: true });
const props = defineProps<{ isAdmin?: boolean }>();
const emit = defineEmits<{ submitted: [] }>();
const submitting = ref(false);
const isolationRequired = ref(false);
const evidence = ref("");
let pollGeneration = 0;
const title = computed(() => isolationRequired.value ? "确认旧任务已隔离" : "确认重新启动任务");
const confirmation = computed(() => isolationRequired.value
  ? "管理员确认后只会释放此任务的旧运行和锁；其它任务与节点不会受影响。"
  : "将请求旧运行收尾后创建新的采集运行；新运行进入采集中前，操作会保持等待。",
);

watch(task, () => {
  pollGeneration += 1;
  isolationRequired.value = false;
  evidence.value = "";
});

function close() {
  if (!submitting.value) { pollGeneration += 1; task.value = undefined; }
}
async function observeIsolation(operation: { id?: string; phase?: string; status?: string }) {
  const current = ++pollGeneration;
  let latest = operation;
  for (let attempt = 0; attempt < 12 && current === pollGeneration && task.value; attempt += 1) {
    if (latest.phase === "ISOLATION_REQUIRED") {
      if (props.isAdmin) isolationRequired.value = true;
      else ElMessage.warning("旧实例需要管理员确认隔离，请联系管理员处理");
      return;
    }
    if (!latest.id || ["SUCCEEDED", "FAILED", "CANCELLED"].includes(String((latest as { status?: string }).status))) return;
    await new Promise(resolve => setTimeout(resolve, 500));
    if (current !== pollGeneration || !task.value) return;
    latest = await api.operationStatus(latest.id);
    if (current !== pollGeneration || !task.value) return;
    if (["FAILED", "CANCELLED"].includes(String(latest.status))) {
      ElMessage.error("重新启动操作未完成，请查看任务状态后重试");
      return;
    }
  }
}
onBeforeUnmount(() => { pollGeneration += 1; });
async function submit() {
  if (!task.value || submitting.value) return;
  const trimmedEvidence = evidence.value.trim();
  if (isolationRequired.value && (trimmedEvidence.length < 10 || trimmedEvidence.length > 2000)) {
    ElMessage.warning("请填写 10 至 2000 字符的隔离证据");
    return;
  }
  submitting.value = true;
  try {
    const operation = await api.restartBlocked(task.value.id, isolationRequired.value
      ? { confirmIsolation: true, evidence: trimmedEvidence }
      : {});
    if (!isolationRequired.value) {
      await observeIsolation(operation as { id?: string; phase?: string; status?: string });
      if (isolationRequired.value || !task.value) return;
    }
    ElMessage.success(isolationRequired.value ? "隔离确认已提交，正在等待新运行采集" : "重新启动请求已提交");
    task.value = undefined;
    emit("submitted");
  } catch (error) {
    if (error instanceof ApiError && error.code === "ISOLATION_REQUIRED" && props.isAdmin) {
      isolationRequired.value = true;
      ElMessage.warning("旧节点不可达，需要确认该任务的旧实例已经隔离");
    } else {
      ElMessage.error(error instanceof Error ? error.message : "重新启动失败");
    }
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <el-dialog :model-value="Boolean(task)" :title="title" width="min(520px, calc(100vw - 32px))" :close-on-click-modal="false" @close="close">
    <p>{{ confirmation }}</p>
    <el-form v-if="isolationRequired" label-position="top">
      <el-form-item label="隔离证据" required>
        <el-input v-model="evidence" type="textarea" :rows="5" maxlength="2000" show-word-limit placeholder="说明该任务旧实例已停止或隔离的核验依据" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button :disabled="submitting" @click="close">取消</el-button>
      <el-button type="danger" :loading="submitting" @click="submit">{{ isolationRequired ? "确认隔离并重新启动" : "确认重新启动" }}</el-button>
    </template>
  </el-dialog>
</template>
