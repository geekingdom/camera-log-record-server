<script setup lang="ts">
import { ref } from "vue";
import UserManager from "./UserManager.vue";
import AccessManager from "./AccessManager.vue";
import IpPolicyManager from "./IpPolicyManager.vue";
const tab = ref("users");
</script>
<template>
  <section>
    <el-tabs v-model="tab"
      ><el-tab-pane label="用户账号" name="users"><UserManager /></el-tab-pane
      ><el-tab-pane label="第三方服务账号" name="tokens"
        ><AccessManager /></el-tab-pane
      ><el-tab-pane label="IP 访问控制" name="ip"
        ><IpPolicyManager /></el-tab-pane
    ></el-tabs>
  </section>
</template>
