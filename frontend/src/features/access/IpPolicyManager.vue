<script setup lang="ts">
// IP 访问控制：按浏览器或第三方客户端来源 IP 限制权限，不关联设备或串口服务器地址。
import { computed, onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import {
  ApiError,
  ipPolicyApi,
  usersApi,
  type IpPolicy,
} from "../../shared/api";
type PolicyRule = IpPolicy["rules"][number];
const policy = ref<IpPolicy>();
const permissionScopes = ref<{ value: string; label: string }[]>([]);
const loading = ref(false);
const saving = ref(false);
const loadError = ref("");
const scopeOptions = computed(() => {
  const options = [
    { value: "*", label: "全部权限" },
    { value: "admin", label: "管理权限" },
    ...permissionScopes.value,
  ];
  return options.filter(
    (option, index) =>
      options.findIndex(({ value }) => value === option.value) === index,
  );
});
// 同时读取策略和权限目录，确保规则中的范围显示为业务中文名称。
async function load() {
  loading.value = true;
  loadError.value = "";
  try {
    const [nextPolicy, permissions] = await Promise.all([
      ipPolicyApi.get(),
      usersApi.permissions(),
    ]);
    policy.value = nextPolicy;
    permissionScopes.value = permissions.scopes;
  } catch (error) {
    loadError.value =
      error instanceof Error ? error.message : "读取 IP 访问控制失败";
  } finally {
    loading.value = false;
  }
}
function add() {
  policy.value?.rules.push({ label: "", network: "", scopes: [] });
}
// “全部权限”与细分权限不可并存；细分权限存在时禁用“全部权限”。
function normalizeScopes(rule: PolicyRule) {
  if (rule.scopes.includes("*")) rule.scopes = ["*"];
}
function allScopeSelected(rule: PolicyRule) {
  return rule.scopes.includes("*");
}
async function remove(index: number) {
  if (!policy.value) return;
  try {
    await ElMessageBox.confirm(
      "确认删除此客户端 IP / 网段规则吗？",
      "确认删除",
      { type: "warning", confirmButtonText: "确认" },
    );
    policy.value.rules.splice(index, 1);
  } catch (error) {
    if (error !== "cancel" && error !== "close")
      ElMessage.error("删除规则失败");
  }
}
async function save() {
  if (!policy.value) return;
  if (
    policy.value.enabled &&
    !policy.value.rules.some((rule) => rule.network.trim())
  )
    return ElMessage.warning("启用白名单前请至少填写一条客户端 IP / 网段规则");
  try {
    await ElMessageBox.confirm(
      "确认保存 IP 访问控制策略吗？当前客户端失去管理权限时服务会拒绝保存。",
      "确认保存",
      { type: "warning", confirmButtonText: "确认" },
    );
    saving.value = true;
    policy.value = await ipPolicyApi.update(policy.value);
    ElMessage.success("IP 访问控制已保存");
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      await load();
      ElMessage.warning("策略已被其他管理员修改，已刷新最新版本");
    } else if (error !== "cancel" && error !== "close")
      ElMessage.error(error instanceof Error ? error.message : "保存策略失败");
  } finally {
    saving.value = false;
  }
}
onMounted(() => void load());
</script>
<template>
  <section class="ip-policy">
    <div class="access-toolbar">
      <div>
        <h2>IP 访问控制</h2>
        <p>当前客户端 IP：{{ policy?.clientIp || "未识别" }}</p>
      </div>
      <el-switch
        v-if="policy"
        v-model="policy.enabled"
        active-text="启用白名单"
        :disabled="loading || saving"
      />
    </div>
    <el-alert
      v-if="loadError"
      type="error"
      :title="loadError"
      show-icon
      :closable="false"
      class="load-error"
      ><template #default
        ><el-button text type="primary" @click="load">重试</el-button></template
      ></el-alert
    ><el-table
      v-if="policy"
      v-loading="loading"
      :data="policy.rules"
      class="data-table"
      ><el-table-column label="名称" min-width="140"
        ><template #default="{ row }"
          ><el-input
            v-model="row.label"
            aria-label="规则名称"
            :disabled="saving" /></template></el-table-column
      ><el-table-column label="客户端 IP / 网段" min-width="210"
        ><template #default="{ row }"
          ><el-input
            v-model="row.network"
            aria-label="客户端 IP / 网段"
            placeholder="192.0.2.10 或 2001:db8::/32"
            :disabled="saving" /></template></el-table-column
      ><el-table-column label="允许权限" min-width="300"
        ><template #default="{ row }"
          ><el-select
            v-model="row.scopes"
            multiple
            aria-label="允许权限"
            :disabled="saving"
            style="width: 100%"
            @change="normalizeScopes(row)"
            ><el-option
              label="全部权限"
              value="*"
              :disabled="
                row.scopes.length > 0 && !allScopeSelected(row)
              " /><el-option
              v-for="scope in scopeOptions.filter(
                (option) => option.value !== '*',
              )"
              :key="scope.value"
              :label="scope.label"
              :value="scope.value"
              :disabled="
                allScopeSelected(row)
              " /></el-select></template></el-table-column
      ><el-table-column label="操作" width="80"
        ><template #default="{ $index }"
          ><el-button
            text
            type="danger"
            :disabled="saving"
            @click="remove($index)"
            >删除</el-button
          ></template
        ></el-table-column
      ></el-table
    >
    <div v-if="policy" class="actions">
      <el-button :disabled="saving" @click="add">新增规则</el-button
      ><el-button type="primary" :loading="saving" @click="save"
        >保存策略</el-button
      >
    </div>
  </section>
</template>
<style scoped>
.access-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-bottom: 20px;
}
.access-toolbar h2 {
  margin: 0;
}
.access-toolbar p {
  margin: 8px 0 0;
  color: #829095;
}
.load-error {
  margin-bottom: 16px;
}
.actions {
  display: flex;
  gap: 8px;
  margin-top: 16px;
}
@media (max-width: 540px) {
  .access-toolbar {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
