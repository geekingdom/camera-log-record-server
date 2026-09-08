<script setup lang="ts">
// 模板列表只处理展示、打开编辑器与携带版本号的删除确认。
import { Edit3, Trash2 } from "lucide-vue-next";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "../../shared/api";
import type { Template } from "../../shared/types";
const props = defineProps<{ items: Template[] }>();
const emit = defineEmits<{ edit: [Template]; changed: [] }>();
async function remove(template: Template) {
  try {
    await ElMessageBox.confirm(`删除模板“${template.name}”？`, "删除模板", {
      type: "warning",
    });
    await api.deleteTemplate(template.id, template.version ?? 1);
    emit("changed");
  } catch (error) {
    if (error !== "cancel" && error !== "close")
      ElMessage.error(error instanceof Error ? error.message : "删除失败");
  }
}
</script>
<template>
  <el-table :data="props.items" class="data-table" empty-text="暂无模板"
    ><el-table-column
      prop="name"
      label="名称"
      min-width="180" /><el-table-column
      prop="description"
      label="说明"
      min-width="280" /><el-table-column
      prop="version"
      label="版本"
      width="100" /><el-table-column label="操作" width="120"
      ><template #default="{ row }"
        ><el-button text :icon="Edit3" @click="emit('edit', row)" /><el-button
          text
          type="danger"
          :icon="Trash2"
          @click="remove(row)" /></template></el-table-column
  ></el-table>
</template>
