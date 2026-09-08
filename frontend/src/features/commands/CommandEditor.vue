<script setup lang="ts">
// 可复用命令编辑器：输入草稿独立于已保存数组，回车遵守中文输入法 composition 状态。
import { ref } from "vue";
import { ElMessage } from "element-plus";
import { Plus, Trash2, ArrowUp, ArrowDown } from "lucide-vue-next";
import type { InitialCommand, ScheduledCommand } from "../../shared/types";
const initial = defineModel<InitialCommand[]>("initialCommands", {
  required: true,
});
const scheduled = defineModel<ScheduledCommand[]>("scheduledCommands", {
  required: true,
});
const draft = ref(""),
  composing = ref(false);
// 仅在非组合输入阶段接收回车，避免中文候选词确认时意外增加命令。
function addInitial(event?: KeyboardEvent) {
  if (composing.value || event?.isComposing) return;
  event?.preventDefault();
  if (!draft.value.trim()) return;
  if (/[\r\n\0]/.test(draft.value))
    return ElMessage.warning("每项必须为一条单行命令");
  initial.value = [
    ...initial.value,
    {
      command: draft.value,
      newline: "\n",
      delaySeconds: 0,
      prompt: null,
      timeoutSeconds: 30,
    },
  ];
  draft.value = "";
}
function move(index: number, step: number) {
  const items = [...initial.value],
    target = index + step;
  if (target < 0 || target >= items.length) return;
  [items[index], items[target]] = [items[target], items[index]];
  initial.value = items;
}
function validate() {
  if (draft.value.trim()) {
    ElMessage.warning("初始化命令输入框还有未添加的内容，请回车添加或清空");
    return false;
  }
  if (
    [...initial.value, ...scheduled.value].some(
      (item) => !item.command.trim() || /[\r\n\0]/.test(item.command),
    )
  ) {
    ElMessage.warning("命令必须为非空单行文本");
    return false;
  }
  if (
    scheduled.value.some(
      (item) =>
        !Number.isInteger(item.totalExecutions) ||
        item.totalExecutions < 1 ||
        !Number.isInteger(item.intervalSeconds) ||
        item.intervalSeconds < 1,
    )
  ) {
    ElMessage.warning("执行总次数和间隔秒数必须为正整数");
    return false;
  }
  return true;
}
defineExpose({ validate });
</script>
<template>
  <h3>初始化命令</h3>
  <div v-for="(item, index) in initial" :key="index" class="initial-row">
    <span class="command-number">{{ index + 1 }}</span>
    <el-input
      v-model="item.command"
      :aria-label="'初始化命令 ' + (index + 1)"
    />
    <el-tooltip content="上移"
      ><el-button
        :icon="ArrowUp"
        :disabled="index === 0"
        aria-label="上移"
        @click="move(index, -1)"
    /></el-tooltip>
    <el-tooltip content="下移"
      ><el-button
        :icon="ArrowDown"
        :disabled="index === initial.length - 1"
        aria-label="下移"
        @click="move(index, 1)"
    /></el-tooltip>
    <el-tooltip content="删除"
      ><el-button
        :icon="Trash2"
        aria-label="删除初始化命令"
        @click="initial = initial.filter((_, position) => position !== index)"
    /></el-tooltip>
  </div>
  <div class="command-entry">
    <el-input
      v-model="draft"
      aria-label="新增初始化命令"
      placeholder="初始化命令"
      @compositionstart="composing = true"
      @compositionend="composing = false"
      @keydown.enter="addInitial"
    />
    <el-button :icon="Plus" aria-label="添加初始化命令" @click="addInitial()" />
  </div>
  <el-collapse v-if="initial.length"
    ><el-collapse-item title="初始化高级设置" name="advanced">
      <div v-for="(item, index) in initial" :key="index" class="advanced-row">
        <span>{{ index + 1 }}</span
        ><el-input v-model="item.prompt" placeholder="提示符（可选）" />
        <el-input-number
          v-model="item.delaySeconds"
          :min="0"
          :max="3600"
          aria-label="发送后延时秒数"
        />
        <el-input-number
          v-model="item.timeoutSeconds"
          :min="1"
          :max="3600"
          aria-label="等待超时秒数"
        />
      </div> </el-collapse-item
  ></el-collapse>
  <h3>定时命令</h3>
  <div
    v-for="(item, index) in scheduled"
    :key="index"
    class="schedule-edit-row"
  >
    <el-form-item label="命令"
      ><el-input v-model="item.command"
    /></el-form-item>
    <el-form-item label="执行总次数"
      ><el-input-number
        v-model="item.totalExecutions"
        :min="1"
        :precision="0"
        controls-position="right"
    /></el-form-item>
    <el-form-item label="间隔（秒）"
      ><el-input-number
        v-model="item.intervalSeconds"
        :min="1"
        :precision="0"
        controls-position="right"
    /></el-form-item>
    <el-tooltip content="删除"
      ><el-button
        :icon="Trash2"
        aria-label="删除定时命令"
        @click="
          scheduled = scheduled.filter((_, position) => position !== index)
        "
    /></el-tooltip>
  </div>
  <el-button
    :icon="Plus"
    @click="
      scheduled = [
        ...scheduled,
        { command: '', totalExecutions: 1, intervalSeconds: 60 },
      ]
    "
    >添加定时命令</el-button
  >
</template>
<style scoped>
.initial-row,
.command-entry,
.advanced-row {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
}
.initial-row .el-input,
.command-entry .el-input {
  flex: 1;
  min-width: 0;
}
.command-number {
  width: 20px;
  flex-shrink: 0;
  color: #647067;
}
.schedule-edit-row {
  display: grid;
  grid-template-columns: minmax(150px, 1fr) 150px 150px 36px;
  gap: 8px;
  align-items: center;
}
.initial-row .el-button + .el-button {
  margin-left: 0;
}
@media (max-width: 640px) {
  .schedule-edit-row {
    grid-template-columns: 1fr 1fr;
  }
  .schedule-edit-row .el-form-item:first-child {
    grid-column: 1/-1;
  }
  .advanced-row {
    flex-wrap: wrap;
  }
}
</style>
