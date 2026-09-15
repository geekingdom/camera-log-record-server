<script setup lang="ts">
// 管理员编辑增长记录的归档期限；保存返回完整平台配置以同步并发版本号。
import { ref, watch } from "vue";
import { Database, Save } from "lucide-vue-next";
import { ElMessage } from "element-plus";
import { confirmAction } from "../../shared/confirm";
import { settingsApi, type PlatformSettings, type RecordRetentionConfig } from "./api";

const props = defineProps<{ settings?: PlatformSettings; loading?: boolean }>();
const emit = defineEmits<{ saved: [settings: PlatformSettings] }>();
const saving = ref(false);
const draft = ref<RecordRetentionConfig>({ auditDays: 90, eventDays: 90, runDays: 90 });
watch(() => props.settings, value => {
  draft.value = { auditDays: value?.recordRetention?.auditDays ?? 90, eventDays: value?.recordRetention?.eventDays ?? 90, runDays: value?.recordRetention?.runDays ?? 90 };
}, { immediate: true });

async function save() {
  if (!props.settings || saving.value) return;
  const value = { ...draft.value };
  if (Object.values(value).some(days => !Number.isInteger(days) || days < 0 || days > 3650)) return ElMessage.warning("保留天数必须是 0 到 3650 的整数");
  if (!await confirmAction("确认保存增长型记录保留策略？0 表示关闭对应自动维护。", "确认保存保留策略")) return;
  saving.value = true;
  try {
    emit("saved", await settingsApi.updatePlatform({ retentionDays: props.settings.retentionDays, version: props.settings.version, recordRetention: value }));
    ElMessage.success("增长记录保留策略已保存");
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : "保存增长记录保留策略失败"); }
  finally { saving.value = false; }
}
</script>

<template>
  <section class="record-retention" v-loading="loading" aria-label="增长记录保留策略">
    <div class="heading"><h3><Database :size="18" />增长记录归档与清理</h3><el-button type="primary" :icon="Save" :loading="saving" :disabled="saving || !settings" @click="save">保存策略</el-button></div>
    <p>按保留天数分批归档和清理；活动运行、未知命令及有效幂等引用继续保留。归档摘要不包含命令正文。</p>
    <el-form label-position="top" :disabled="saving" class="fields">
      <el-form-item label="审计记录保留天数"><el-input-number v-model="draft.auditDays" :min="0" :max="3650" :precision="0" aria-label="审计记录保留天数" /><span>0 为关闭</span></el-form-item>
      <el-form-item label="运行事件保留天数"><el-input-number v-model="draft.eventDays" :min="0" :max="3650" :precision="0" aria-label="运行事件保留天数" /><span>0 为关闭</span></el-form-item>
      <el-form-item label="完成运行明细保留天数"><el-input-number v-model="draft.runDays" :min="0" :max="3650" :precision="0" aria-label="完成运行明细保留天数" /><span>0 为关闭</span></el-form-item>
    </el-form>
  </section>
</template>

<style scoped>
.record-retention { min-width: 0; margin-top: 24px; padding-top: 24px; border-top: 1px solid #e5eaeb; }.heading { display:flex; align-items:center; justify-content:space-between; gap:12px; }.heading h3 { display:flex; align-items:center; gap:8px; margin:0; font-size:16px; }.record-retention p { color:#627175; line-height:1.6; }.fields { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:16px; }.fields span { margin-left:8px; color:#829095; font-size:12px; }@media (max-width:720px){.heading{align-items:flex-start;flex-direction:column}.fields{grid-template-columns:1fr}}
</style>
