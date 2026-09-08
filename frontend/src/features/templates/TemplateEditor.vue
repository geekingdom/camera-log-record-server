<script setup lang="ts">
// 命令模板编辑器：模板版本由后端乐观锁控制，命令编辑复用 commands 功能模块。
import { ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { api } from "../../shared/api";
import type { Template } from "../../shared/types";
import CommandEditor from "../commands/CommandEditor.vue";
const open = defineModel<boolean>({ required: true });
const props = defineProps<{ template?: Template }>();
const emit = defineEmits<{ saved: [] }>();
const editorRef = ref<InstanceType<typeof CommandEditor>>();
const saving = ref(false),
  loading = ref(false);
const blank = (): Template => ({
  id: "",
  name: "",
  description: "",
  initialCommands: [],
  scheduledCommands: [],
});
const form = ref<Template>(blank());
let generation = 0;
// 防止抽屉切换期间旧模板详情异步回写到新模板。
watch(
  () => [open.value, props.template] as const,
  async ([visible, template]) => {
    const current = ++generation;
    if (!visible) return;
    loading.value = true;
    try {
      const value = template ? await api.template(template.id) : blank();
      if (current === generation) form.value = value;
    } catch (error) {
      ElMessage.error(error instanceof Error ? error.message : "读取模板失败");
    } finally {
      if (current === generation) loading.value = false;
    }
  },
  { immediate: true },
);
async function save() {
  if (saving.value || !editorRef.value?.validate()) return;
  if (!form.value.name.trim()) return ElMessage.warning("请输入模板名称");
  saving.value = true;
  try {
    if (props.template)
      await api.updateTemplate(props.template.id, {
        ...form.value,
        version: form.value.version ?? 1,
      });
    else await api.createTemplate(form.value);
    ElMessage.success("模板已保存");
    open.value = false;
    emit("saved");
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "保存失败");
  } finally {
    saving.value = false;
  }
}
</script>
<template>
  <el-drawer
    v-model="open"
    :title="props.template ? '编辑命令模板' : '新建命令模板'"
    size="min(900px, 96vw)"
    destroy-on-close
  >
    <el-form label-position="top" class="editor-form" v-loading="loading"
      ><section class="form-section">
        <div class="form-grid">
          <el-form-item label="模板名称" required
            ><el-input v-model="form.name" maxlength="128"
          /></el-form-item>
          <el-form-item label="版本"
            ><el-input :model-value="String(form.version ?? '新版本')" disabled
          /></el-form-item>
        </div>
        <el-form-item label="说明"
          ><el-input
            v-model="form.description"
            type="textarea"
            maxlength="2000"
        /></el-form-item>
      </section>
      <section class="form-section">
        <CommandEditor
          ref="editorRef"
          v-model:initial-commands="form.initialCommands"
          v-model:scheduled-commands="form.scheduledCommands"
        /></section
    ></el-form>
    <template #footer
      ><el-button @click="open = false">取消</el-button
      ><el-button
        type="primary"
        :loading="saving"
        :disabled="loading"
        @click="save"
        >保存模板</el-button
      ></template
    >
  </el-drawer>
</template>
