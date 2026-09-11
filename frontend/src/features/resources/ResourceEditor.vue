<script setup lang="ts">
// 资源编辑器只处理设备 HTTP 认证；采集任务的 SSH/Telnet 凭据始终由任务单独维护。
import { computed, ref, watch } from "vue";
import { ElMessage, type FormInstance } from "element-plus";
import { Activity, Fingerprint, KeyRound, Network, Server, ShieldCheck } from "lucide-vue-next";
import { api } from "../../shared/api";
import { confirmAction } from "../../shared/confirm";
import { usePermissions } from "../../shared/permissions";
import type { Resource, ResourceAuthentication, ResourceAuthType, ResourceKind } from "../../shared/types";

const open = defineModel<boolean>({ required: true });
const props = defineProps<{ resource?: Resource; canEdit?: boolean }>();
const emit = defineEmits<{ saved: [] }>();
const permissions = usePermissions();
const canSave = computed(() => props.resource
  ? Boolean(props.canEdit) && permissions.can("resources:write")
  : permissions.can("resources:create"));
const blank = () => ({ name: "", kind: "HIKVISION_NETWORK" as ResourceKind, ip: "", username: "", password: "", authType: "DIGEST" as ResourceAuthType, enableCoredumpMonitor: false, enableResourceMonitor: false });
const form = ref(blank());
const authenticated = ref<ResourceAuthentication>();
const authenticating = ref(false);
const saving = ref(false);
const loading = ref(false);
const editVersion = ref(1);
const formRef = ref<FormInstance>();
const isNetwork = computed(() => form.value.kind === "HIKVISION_NETWORK");
const healthText = computed(() => ({ ONLINE: "当前连通", AUTH_FAILED: "HTTP 凭据失效", OFFLINE: "设备离线", ERROR: "认证检查异常" }[props.resource?.healthStatus ?? ""] ?? "尚未检查"));
const fingerprint = computed(() => JSON.stringify([form.value.kind, form.value.ip, form.value.username, form.value.password, form.value.authType]));
let verifiedFingerprint = "";
let authenticationGeneration = 0;
let formGeneration = 0;
const rules = computed(() => ({
  name: [{ required: true, whitespace: true, message: "请输入资源名称", trigger: "blur" }],
  ip: [{ required: true, message: "请输入 IP 地址", trigger: "blur" }],
  username: [{ required: isNetwork.value, message: "请输入 HTTP 用户名", trigger: "blur" }],
  password: [{ required: isNetwork.value && !props.resource, message: "请输入 HTTP 密码", trigger: "blur" }],
}));
function invalidateAuthentication() {
  if (fingerprint.value !== verifiedFingerprint) {
    ++authenticationGeneration;
    authenticating.value = false;
    authenticated.value = undefined;
  }
}
watch(fingerprint, invalidateAuthentication);
watch(isNetwork, (network) => {
  if (network) return;
  form.value.username = "";
  form.value.password = "";
  form.value.enableCoredumpMonitor = false;
  form.value.enableResourceMonitor = false;
  authenticated.value = undefined;
  verifiedFingerprint = "";
  ++authenticationGeneration;
});
watch(() => [open.value, props.resource] as const, async ([visible, resource]) => {
  ++formGeneration;
  if (!visible) {
    ++authenticationGeneration;
    authenticating.value = false;
    saving.value = false;
    return;
  }
  if (!canSave.value) { open.value = false; return; }
  form.value = blank();
  authenticated.value = undefined;
  verifiedFingerprint = "";
  ++authenticationGeneration;
  if (resource) {
    const current = formGeneration;
    loading.value = true;
    try {
      const loaded = await api.resource(resource.id);
      if (current !== formGeneration || !open.value) return;
      form.value = { name: loaded.name, kind: loaded.kind, ip: loaded.ip,
        username: loaded.username ?? "", password: "", authType: loaded.authType ?? "DIGEST",
        enableCoredumpMonitor: loaded.enableCoredumpMonitor ?? false,
        enableResourceMonitor: loaded.enableResourceMonitor ?? false };
      editVersion.value = loaded.version ?? 1;
      authenticated.value = loaded;
      verifiedFingerprint = fingerprint.value;
    } catch (error) {
      if (current === formGeneration) ElMessage.error(error instanceof Error ? error.message : "读取资源失败");
    } finally { if (current === formGeneration) loading.value = false; }
  } else loading.value = false;
// 懒加载编辑器在首次挂载时已经打开，需要立即读取原资源，不能只等下次变更。
}, { immediate: true });
async function authenticate() {
  if (!canSave.value) return;
  if (authenticating.value || saving.value) return;
  if (!form.value.password) return ElMessage.warning("重新认证请填写 HTTP 密码");
  if (!(await formRef.value?.validateField(["name", "ip", "username", "password"]).catch(() => false))) return;
  const current = ++authenticationGeneration;
  authenticating.value = true;
  try {
    const result = await api.authenticateResource({ ...form.value }, props.resource?.id);
    if (current !== authenticationGeneration || !open.value) return;
    authenticated.value = result;
    verifiedFingerprint = fingerprint.value;
    ElMessage.success("设备认证成功");
  } catch (error) {
    if (current === authenticationGeneration && open.value) {
      authenticated.value = undefined;
      ElMessage.error(error instanceof Error ? error.message : "设备认证失败");
    }
  } finally { if (current === authenticationGeneration) authenticating.value = false; }
}
async function save() {
  if (!canSave.value) return;
  if (saving.value) return;
  const current = formGeneration;
  if (!(await formRef.value?.validate().catch(() => false))) return;
  if (current !== formGeneration || !open.value || saving.value) return;
  if (isNetwork.value && (!authenticated.value || verifiedFingerprint !== fingerprint.value)) {
    ElMessage.warning("请先使用当前凭据完成认证"); return;
  }
  saving.value = true;
  try {
    const payload = isNetwork.value ? { ...form.value } : {
      name: form.value.name, kind: form.value.kind, ip: form.value.ip,
    };
    if (props.resource) {
      const resourceId = props.resource.id;
      if (!(await confirmAction(`确认保存资源“${form.value.name}”的修改？`, "确认编辑资源"))) return;
      if (current !== formGeneration || !open.value) return;
      await api.updateResource(resourceId, { ...payload, version: editVersion.value });
    } else await api.createResource(payload);
    emit("saved");
    if (current === formGeneration && open.value) {
      ElMessage.success("资源已保存"); open.value = false;
    }
  } catch (error) {
    if (current === formGeneration && open.value)
      ElMessage.error(error instanceof Error ? error.message : "保存资源失败");
  } finally { if (current === formGeneration) saving.value = false; }
}
</script>
<template>
  <el-drawer v-model="open" :title="props.resource ? '编辑设备资源' : '新建设备资源'" size="min(720px, 96vw)" destroy-on-close class="resource-editor-drawer">
    <el-form ref="formRef" :model="form" :rules="rules" :disabled="saving || loading" v-loading="loading" label-position="top" class="editor-form resource-editor-form">
      <section class="form-section resource-editor-section">
        <div class="resource-section-heading"><span class="resource-section-icon"><Network v-if="isNetwork" :size="18" /><Server v-else :size="18" /></span><div><h2>资源连接</h2><p>资源类型和地址确定任务归属与存储身份</p></div></div>
        <div class="form-grid">
          <el-form-item label="资源名称" prop="name"><el-input v-model="form.name" maxlength="128" /></el-form-item>
          <el-form-item label="资源类型"><el-select v-model="form.kind" :disabled="Boolean(props.resource)"><el-option label="海康网络设备" value="HIKVISION_NETWORK" /><el-option label="串口服务器" value="SERIAL_SERVER" /></el-select></el-form-item>
          <el-form-item label="IP 地址" prop="ip"><el-input v-model="form.ip" :disabled="Boolean(props.resource)" /></el-form-item>
        </div>
      </section>
      <section v-if="isNetwork" class="form-section resource-editor-section">
        <div class="resource-section-heading"><span class="resource-section-icon"><Activity :size="18" /></span><div><h2>资源监控</h2><p>采集开关属于设备资源，所有可读用户可查看已采集的历史趋势</p></div></div>
        <div class="resource-monitor-options">
          <div><strong>Coredump 监控</strong><small>采集设备产生的 Coredump 文件</small></div>
          <el-switch v-model="form.enableCoredumpMonitor" aria-label="启用 Coredump 监控" />
          <div><strong>CPU 与内存监控</strong><small>按资源配置采样 CPU 和内存指标</small></div>
          <el-switch v-model="form.enableResourceMonitor" aria-label="启用 CPU 与内存监控" />
        </div>
      </section>
      <section v-if="isNetwork" class="form-section resource-editor-section">
        <div class="resource-section-heading resource-auth-heading"><span class="resource-section-icon verified"><ShieldCheck :size="18" /></span><div><h2>设备 HTTP 认证</h2><p>认证结果用于确认网络设备身份，不作为采集登录凭据</p></div><el-button :loading="authenticating" :icon="KeyRound" @click="authenticate">点击认证</el-button></div>
        <div class="form-grid">
          <el-form-item label="用户名" prop="username"><el-input v-model="form.username" autocomplete="off" /></el-form-item>
          <el-form-item :label="props.resource ? '密码（留空保持原值）' : '密码'" prop="password"><el-input v-model="form.password" type="password" show-password autocomplete="new-password" /></el-form-item>
          <el-form-item label="认证方式"><el-select v-model="form.authType"><el-option label="摘要认证" value="DIGEST" /><el-option label="基础认证" value="BASIC" /></el-select></el-form-item>
        </div>
        <div v-if="props.resource" class="resource-auth-result" role="status"><div class="resource-auth-title"><Fingerprint :size="17" /><strong>已保存设备身份</strong></div><div><span>当前检查</span><strong>{{ healthText }}</strong></div><div><span>设备型号</span><strong>{{ props.resource.model || "未返回" }}</strong></div><div><span>设备序列号</span><strong>{{ props.resource.subSerialNumber || "未返回" }}</strong></div></div>
        <div v-if="authenticated && verifiedFingerprint === fingerprint" class="resource-auth-result" role="status"><div class="resource-auth-title"><ShieldCheck :size="17" /><strong>本次认证结果</strong></div><div><span>设备型号</span><strong>{{ authenticated.model || "未返回" }}</strong></div><div><span>设备序列号</span><strong>{{ authenticated.subSerialNumber || "未返回" }}</strong></div><div><span>软件版本</span><strong>{{ authenticated.softwareVersion || "未返回" }}</strong></div></div>
      </section>
    </el-form>
    <template #footer><el-button @click="open = false">关闭</el-button><el-button type="primary" :loading="saving" :disabled="loading || (isNetwork && (!authenticated || verifiedFingerprint !== fingerprint))" @click="save">保存资源</el-button></template>
  </el-drawer>
</template>
<style scoped>
.resource-monitor-options { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 12px 16px; align-items: center; }
.resource-monitor-options small { display: block; margin-top: 4px; color: var(--el-text-color-secondary); }
</style>
