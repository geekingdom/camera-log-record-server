<script setup lang="ts">
// 管理员编辑资源监控规则；草稿与已保存版本隔离，提交前二次确认。
import { ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { Cpu, Plus, Save, Trash2 } from "lucide-vue-next";
import { idempotencyKey } from "../../shared/api";
import { confirmAction } from "../../shared/confirm";
import { settingsApi, type PlatformSettings, type ResourceMonitorConfig } from "./api";

const props = defineProps<{ settings?: PlatformSettings; loading?: boolean }>();
const emit = defineEmits<{ saved: [settings: PlatformSettings] }>();
const draft = ref<ResourceMonitorConfig>();
const saving = ref(false);
const revision = ref(0);
watch(() => props.settings, value => {
  revision.value += 1;
  draft.value = value?.resourceMonitor ? JSON.parse(JSON.stringify(value.resourceMonitor)) : undefined;
}, { immediate: true });

function addItem() {
  draft.value?.items.push({ id: idempotencyKey(), name: "", command: "", pattern: "", unit: "KB", enabled: true });
}
function addProcess() {
  draft.value?.processRules.push({ id: idempotencyKey(), name: "", pattern: "", nameGroup: null, enabled: true });
}
async function removeRule(kind: "items" | "processRules", index: number) {
  if (!draft.value || saving.value) return;
  const current = revision.value;
  const item = draft.value[kind][index];
  if (!item || !await confirmAction(`确认删除规则“${item.name || "未命名"}”？保存配置后生效。`, "确认删除规则")) return;
  if (current === revision.value && draft.value[kind][index] === item) draft.value[kind].splice(index, 1);
}
async function save() {
  if (!draft.value || !props.settings || saving.value) return;
  const current = revision.value;
  const config: ResourceMonitorConfig = JSON.parse(JSON.stringify(draft.value));
  if (config.items.some(item => !item.name.trim() || !item.command.trim() || !item.pattern.trim())
      || config.processRules.some(item => !item.name.trim() || !item.pattern.trim())) {
    ElMessage.warning("请填写完整的指标名称、命令和匹配规则"); return;
  }
  if (!await confirmAction("确认保存 CPU 与内存监控规则？已启用资源将在下一轮采样使用新规则。", "确认保存监控配置")) return;
  if (current !== revision.value || !props.settings) return;
  saving.value = true;
  try {
    const updated = await settingsApi.updatePlatform({
      retentionDays: props.settings.retentionDays, version: props.settings.version, resourceMonitor: config,
    });
    emit("saved", updated);
    ElMessage.success("监控配置已保存");
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "监控配置保存失败");
  } finally { saving.value = false; }
}
</script>

<template>
  <section class="monitor-settings" v-loading="loading" aria-label="CPU 与内存监控配置">
    <div class="monitor-settings-heading">
      <h3><Cpu :size="18" />CPU 与内存监控</h3>
      <el-button type="primary" :icon="Save" :loading="saving" :disabled="!draft || saving" @click="save">保存监控配置</el-button>
    </div>
    <el-empty v-if="!draft && !loading" description="监控配置暂不可用" :image-size="60" />
    <el-form v-if="draft" label-position="top" :disabled="saving" class="monitor-rules-form">
      <div class="monitor-base-grid">
        <el-form-item label="采样周期"><el-input-number :model-value="draft.intervalSeconds" disabled /><span class="field-unit">秒</span></el-form-item>
        <el-form-item label="指标保留天数"><el-input-number v-model="draft.retentionDays" :min="1" :max="90" :precision="0" aria-label="指标保留天数" /></el-form-item>
      </div>
      <div class="monitor-rule-heading"><h4>系统指标</h4><el-button :icon="Plus" :disabled="draft.items.length >= 16" @click="addItem">新增指标</el-button></div>
      <div class="monitor-rule-list">
        <div v-for="(item, index) in draft.items" :key="item.id" class="monitor-rule">
          <div class="monitor-rule-title"><el-switch v-model="item.enabled" :aria-label="`启用指标 ${item.name || index + 1}`" /><strong>{{ item.name || `指标 ${index + 1}` }}</strong><el-tooltip content="删除指标"><el-button text type="danger" :icon="Trash2" :aria-label="`删除指标 ${index + 1}`" @click="removeRule('items', index)" /></el-tooltip></div>
          <div class="monitor-rule-grid">
            <el-form-item label="指标名称"><el-input v-model="item.name" maxlength="96" :aria-label="`指标名称 ${index + 1}`" /></el-form-item>
            <el-form-item label="单位"><el-select v-model="item.unit" :aria-label="`指标单位 ${index + 1}`"><el-option label="KB" value="KB" /><el-option label="%" value="%" /></el-select></el-form-item>
            <el-form-item label="采集命令" class="wide"><el-input v-model="item.command" maxlength="2048" :aria-label="`采集命令 ${index + 1}`" /></el-form-item>
            <el-form-item label="数值正则（第 1 捕获组）" class="wide"><el-input v-model="item.pattern" maxlength="512" :aria-label="`数值正则 ${index + 1}`" /></el-form-item>
          </div>
        </div>
      </div>
      <div class="monitor-rule-heading"><h4>进程内存</h4><el-button :icon="Plus" :disabled="draft.processRules.length >= 8" @click="addProcess">新增进程规则</el-button></div>
      <div class="monitor-rule-grid">
        <el-form-item label="进程列表命令" class="wide"><el-input v-model="draft.processDiscoveryCommand" maxlength="2048" aria-label="进程列表命令" /></el-form-item>
        <el-form-item label="进程状态命令（{pid} 为进程 ID）" class="wide"><el-input v-model="draft.processStatusCommand" maxlength="2048" aria-label="进程状态命令" /></el-form-item>
        <el-form-item label="进程内存数值正则（第 1 捕获组）" class="wide"><el-input v-model="draft.processValuePattern" maxlength="512" aria-label="进程内存数值正则" /></el-form-item>
      </div>
      <div class="monitor-rule-list">
        <div v-for="(rule, index) in draft.processRules" :key="rule.id" class="monitor-rule">
          <div class="monitor-rule-title"><el-switch v-model="rule.enabled" :aria-label="`启用进程规则 ${index + 1}`" /><strong>{{ rule.name || `进程规则 ${index + 1}` }}</strong><el-tooltip content="删除进程规则"><el-button text type="danger" :icon="Trash2" :aria-label="`删除进程规则 ${index + 1}`" @click="removeRule('processRules', index)" /></el-tooltip></div>
          <div class="monitor-rule-grid">
            <el-form-item label="进程指标名称"><el-input v-model="rule.name" maxlength="96" :aria-label="`进程指标名称 ${index + 1}`" /></el-form-item>
            <el-form-item label="动态名称捕获组（可空）"><el-input-number v-model="rule.nameGroup" :min="1" :max="16" :precision="0" :aria-label="`动态名称捕获组 ${index + 1}`" /></el-form-item>
            <el-form-item label="进程命令匹配正则" class="wide"><el-input v-model="rule.pattern" maxlength="512" :aria-label="`进程匹配正则 ${index + 1}`" /></el-form-item>
          </div>
        </div>
      </div>
    </el-form>
  </section>
</template>

<style scoped>
.monitor-settings { min-width: 0; margin-top: 24px; padding-top: 24px; border-top: 1px solid #e5eaeb; }
.monitor-settings-heading, .monitor-rule-heading, .monitor-rule-title { display: flex; gap: 12px; align-items: center; justify-content: space-between; }
.monitor-settings h3 { display: flex; align-items: center; gap: 8px; margin: 0; font-size: 16px; }
.monitor-rule-heading { margin: 20px 0 12px; }.monitor-rule-heading h4 { margin: 0; font-size: 14px; }
.monitor-base-grid, .monitor-rule-grid { display: grid; grid-template-columns: minmax(0, 2fr) minmax(140px, 1fr); gap: 0 16px; }
.monitor-base-grid { max-width: 680px; margin-top: 20px; }.field-unit { margin-left: 8px; }
.monitor-rule-list { display: grid; gap: 12px; }.monitor-rule { min-width: 0; border: 1px solid #dce5e5; border-radius: 6px; padding: 12px 16px 0; }
.monitor-rule-title { justify-content: flex-start; margin-bottom: 12px; }.monitor-rule-title strong { flex: 1; min-width: 0; overflow-wrap: anywhere; font-size: 13px; }
.wide { grid-column: 1 / -1; }.monitor-rules-form :deep(.el-input__inner) { font-family: ui-monospace, monospace; }
@media (max-width: 620px) { .monitor-settings-heading { align-items: flex-start; flex-direction: column; }.monitor-base-grid, .monitor-rule-grid { grid-template-columns: minmax(0, 1fr); } }
</style>
