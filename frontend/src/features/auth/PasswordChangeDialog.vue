<script setup lang="ts">
// 改密对话框不保存成功后的口令；关闭、成功和退出均立即清除两个输入值。
import { ref, watch } from "vue";

const props = defineProps<{
  open: boolean;
  required: boolean;
  saving: boolean;
  changePassword(currentPassword: string, nextPassword: string): Promise<boolean>;
}>();
const emit = defineEmits<{
  "update:open": [value: boolean];
  logout: [];
}>();
const currentPassword = ref("");
const nextPassword = ref("");

function clear() {
  currentPassword.value = "";
  nextPassword.value = "";
}
function close() {
  clear();
  emit("update:open", false);
}
async function save() {
  if (await props.changePassword(currentPassword.value, nextPassword.value)) clear();
}
watch(
  () => props.open,
  (open) => {
    if (!open) clear();
  },
);
</script>

<template>
  <el-dialog
    :model-value="open"
    title="修改密码"
    width="min(440px,94vw)"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    :show-close="false"
    @update:model-value="close"
    ><el-form label-position="top"
      ><el-form-item label="当前密码"
        ><el-input
          v-model="currentPassword"
          type="password"
          show-password
          autocomplete="current-password" /></el-form-item
      ><el-form-item label="新密码"
        ><el-input
          v-model="nextPassword"
          type="password"
          show-password
          autocomplete="new-password" /></el-form-item></el-form
    ><template #footer
      ><el-button
        v-if="required"
        @click="
          clear();
          emit('logout');
        "
        >退出登录</el-button
      ><el-button v-else @click="close">取消</el-button
      ><el-button type="primary" :loading="saving" @click="save"
        >保存新密码</el-button
      ></template
    ></el-dialog
  >
</template>
