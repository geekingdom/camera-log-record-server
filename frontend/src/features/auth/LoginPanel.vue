<script setup lang="ts">
// 登录面板只保留输入态；认证网络请求和会话副作用由 usePlatformSession 统一处理。
import { ref } from "vue";
import { KeyRound, Terminal } from "lucide-vue-next";

const props = defineProps<{
  busy: boolean;
  restoring: boolean;
  restoreError?: string;
  login(username: string, password: string): Promise<boolean>;
  retryRestore(): Promise<void>;
}>();
const username = ref("");
const password = ref("");

async function submit() {
  if (await props.login(username.value, password.value)) password.value = "";
}
</script>

<template>
  <section class="login-state">
    <div class="login-symbol"><Terminal :size="30" /></div>
    <h1>设备日志服务</h1>
    <p v-if="restoring" class="login-recovery-state" role="status">正在恢复登录会话…</p>
    <template v-else>
      <p v-if="restoreError" class="login-recovery-error" role="alert">会话恢复失败：{{ restoreError }}</p>
      <el-button v-if="restoreError" plain @click="retryRestore">重试恢复会话</el-button>
    </template>
    <form class="auth" @submit.prevent="submit">
      <el-input
        v-model="username"
        placeholder="用户名"
        aria-label="用户名"
        autocomplete="username"
        :disabled="restoring"
      /><el-input
        v-model="password"
        type="password"
        placeholder="密码"
        aria-label="密码"
        autocomplete="current-password"
        show-password
        :disabled="restoring"
      /><el-button native-type="submit" type="primary" :icon="KeyRound" :loading="busy" :disabled="restoring"
        >登录</el-button
      >
    </form>
  </section>
</template>
