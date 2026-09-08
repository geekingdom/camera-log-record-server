<script setup lang="ts">
// 任务编辑器管理版本化配置保存和工作区切换；日志、归档、命令记录保持为独立功能模块。
import { computed, ref, watch } from "vue";
import { ElMessage, ElMessageBox, type FormInstance } from "element-plus";
import { api } from "../../shared/api";
import type { Task, Template } from "../../shared/types";
import CommandEditor from "../commands/CommandEditor.vue";
import LiveLogs from "../logs/LiveLogs.vue";
import LogArchives from "../logs/LogArchives.vue";
import CommandHistory from "../commands/CommandHistory.vue";
const open = defineModel<boolean>({ required: true });
const props = defineProps<{ task?: Task; templates: Template[]; initialWorkspace?: string }>();
const emit = defineEmits<{ saved: [] }>();
const blank = (): Task => ({
  id: "",
  name: "",
  protocol: "SSH",
  ip: "",
  port: 22,
  username: "",
  password: "",
  initialCommands: [],
  scheduledCommands: [],
});
const form = ref<Task>(blank()),
  original = ref<Task>(blank());
const formRef = ref<FormInstance>(),
  editorRef = ref<InstanceType<typeof CommandEditor>>();
const autoStart = ref(false),
  clearPassword = ref(false),
  templateId = ref(""),
  templateItems = ref<Template[]>([]),
  templatePage = ref(1),
  templateTotal = ref(0),
  templateLoading = ref(false),
  saving = ref(false),
  loading = ref(false);
const templatePageSize = 20;
const portTouched = ref(false),
  workspace = ref("config"),
  serial = computed(() => form.value.protocol === "TELNET_SERIAL");
let generation = 0;
let templateGeneration = 0;
// 模板选择器独立分页；序号保证抽屉关闭、重开或翻页时旧响应不会覆盖当前页。
async function loadTemplatePage(page: number) {
  const current = ++templateGeneration;
  templateLoading.value = true;
  try {
    const response = await api.templates(page, templatePageSize);
    if (current !== templateGeneration || !open.value) return;
    templateItems.value = response.items;
    templatePage.value = response.page;
    templateTotal.value = response.total;
  } catch (error) {
    if (current === templateGeneration && open.value)
      ElMessage.error(error instanceof Error ? error.message : "读取命令模板失败");
  } finally {
    if (current === templateGeneration) templateLoading.value = false;
  }
}
const rules = computed(() => ({
  name: [
    {
      required: true,
      whitespace: true,
      message: "请输入任务名称",
      trigger: "blur",
    },
  ],
  ip: [{ required: true, message: "请输入 IP 地址", trigger: "blur" }],
  port: [
    {
      required: true,
      type: "number" as const,
      min: 1,
      max: 65535,
      message: "端口范围为 1–65535",
      trigger: "change",
    },
  ],
  username: [
    { required: !serial.value, message: "此协议需要用户名", trigger: "blur" },
  ],
  password: [
    {
      required: !serial.value && !props.task,
      message: "此协议需要密码",
      trigger: "blur",
    },
  ],
}));
// generation 防止快速切换任务时较慢的详情请求覆盖当前编辑表单。
watch(
  () => [open.value, props.task] as const,
  async ([visible, task]) => {
    const current = ++generation;
    ++templateGeneration;
    if (!visible) {
      templateLoading.value = false;
      return;
    }
    loading.value = true;
    workspace.value = props.initialWorkspace ?? "config";
    clearPassword.value = false;
    templateId.value = "";
    templateItems.value = [];
    templatePage.value = 1;
    templateTotal.value = 0;
    void loadTemplatePage(1);
    autoStart.value = false;
    portTouched.value = Boolean(task);
    try {
      const loaded = task ? await api.task(task.id) : blank();
      if (current !== generation) return;
      loaded.password = "";
      form.value = structuredClone(loaded);
      original.value = structuredClone(loaded);
    } catch (error) {
      ElMessage.error(error instanceof Error ? error.message : "读取任务失败");
    } finally {
      if (current === generation) loading.value = false;
    }
  },
  { immediate: true },
);
watch(
  () => form.value.protocol,
  (protocol) => {
    if (!props.task && !portTouched.value)
      form.value.port =
        protocol === "SSH" ? 22 : protocol === "TELNET_DEVICE" ? 23 : undefined;
    if (protocol !== "TELNET_SERIAL") clearPassword.value = false;
  },
);
async function replaceTemplate() {
  const template = templateItems.value.find((item) => item.id === templateId.value);
  if (!template) return;
  if (
    (form.value.initialCommands.length ||
      form.value.scheduledCommands.length) &&
    JSON.stringify([
      form.value.initialCommands,
      form.value.scheduledCommands,
    ]) !==
      JSON.stringify([template.initialCommands, template.scheduledCommands])
  ) {
    try {
      await ElMessageBox.confirm(
        "将使用模板替换当前命令配置。",
        "替换命令配置",
        { type: "warning" },
      );
    } catch {
      templateId.value = "";
      return;
    }
  }
  form.value.initialCommands = JSON.parse(
    JSON.stringify(template.initialCommands),
  );
  form.value.scheduledCommands = JSON.parse(
    JSON.stringify(template.scheduledCommands),
  );
  form.value.sourceTemplateId = template.id;
  form.value.sourceTemplateVersion = template.version;
}
// 仅提交与初始快照不同的字段，保留编辑密码为空时“不覆盖原密码”的后端语义。
async function save() {
  if (
    saving.value ||
    !editorRef.value?.validate() ||
    !(await formRef.value?.validate().catch(() => false))
  )
    return;
  saving.value = true;
  try {
    const payload = JSON.parse(JSON.stringify(form.value)) as Task;
    if (props.task) {
      const changes = Object.fromEntries(
        Object.entries(payload).filter(
          ([key, value]) =>
            JSON.stringify(value) !==
            JSON.stringify(original.value[key as keyof Task]),
        ),
      );
      if (clearPassword.value) changes.clearPassword = true;
      if (
        props.task.desiredState === "RUNNING" &&
        Object.keys(changes).some(
          (key) => !["name", "description"].includes(key),
        )
      ) {
        try {
          await ElMessageBox.confirm(
            "修改连接或命令将重新开启任务，定时命令次数从零计算。",
            "应用运行配置",
          );
        } catch {
          return;
        }
      }
      await api.updateTask(props.task.id, {
        ...changes,
        version: payload.version ?? 1,
      });
    } else await api.createTask(payload, autoStart.value);
    ElMessage.success("任务已保存");
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
    :title="props.task ? '任务 · ' + props.task.name : '新建采集任务'"
    size="min(1000px, 96vw)"
    destroy-on-close
  >
    <el-tabs v-model="workspace">
      <el-tab-pane label="任务配置" name="config">
        <el-form
          ref="formRef"
          :model="form"
          :rules="rules"
          label-position="top"
          class="editor-form"
          v-loading="loading"
        >
          <section class="form-section">
            <h2>基本连接</h2>
            <div class="form-grid">
              <el-form-item label="任务名称" prop="name"
                ><el-input v-model="form.name" maxlength="128"
              /></el-form-item>
              <el-form-item label="连接协议" required
                ><el-select v-model="form.protocol"
                  ><el-option label="SSH" value="SSH" /><el-option
                    label="Telnet 设备"
                    value="TELNET_DEVICE" /><el-option
                    label="Telnet 串口"
                    value="TELNET_SERIAL" /></el-select
              ></el-form-item>
              <el-form-item
                :label="serial ? '串口服务器 IP' : '设备 IP'"
                prop="ip"
                ><el-input v-model="form.ip"
              /></el-form-item>
              <el-form-item label="端口" prop="port"
                ><el-input-number
                  v-model="form.port"
                  :min="1"
                  :max="65535"
                  controls-position="right"
                  @change="portTouched = true"
              /></el-form-item>
              <el-form-item label="用户名" prop="username"
                ><el-input v-model="form.username" autocomplete="off"
              /></el-form-item>
              <el-form-item
                :label="props.task ? '密码（留空保持原值）' : '密码'"
                prop="password"
                ><el-input
                  v-model="form.password"
                  type="password"
                  show-password
                  autocomplete="new-password"
                  :disabled="clearPassword"
              /></el-form-item>
            </div>
            <el-checkbox v-if="serial && props.task" v-model="clearPassword"
              >清除已保存密码</el-checkbox
            ><el-checkbox v-if="!props.task" v-model="autoStart"
              >保存后立即启动</el-checkbox
            >
          </section>
          <section class="form-section">
            <div class="section-heading">
              <h2>命令配置</h2>
              <el-select
                v-model="templateId"
                clearable
                :loading="templateLoading"
                placeholder="选择命令模板"
                @change="replaceTemplate"
                ><el-option
                  v-for="item in templateItems"
                  :key="item.id"
                  :label="item.name"
                  :value="item.id"
              /><template #footer>
                <el-pagination
                  v-if="templateTotal > templatePageSize"
                  v-model:current-page="templatePage"
                  :disabled="templateLoading"
                  :page-size="templatePageSize"
                  :total="templateTotal"
                  layout="prev, pager, next"
                  small
                  @current-change="loadTemplatePage"
                />
              </template></el-select>
            </div>
            <CommandEditor
              ref="editorRef"
              v-model:initial-commands="form.initialCommands"
              v-model:scheduled-commands="form.scheduledCommands"
            />
          </section>
        </el-form>
      </el-tab-pane>
      <el-tab-pane v-if="props.task" label="实时打印" name="live"
        ><LiveLogs v-if="open && workspace === 'live'" :task-id="props.task.id" @history="workspace = 'archives'"
      /></el-tab-pane>
      <el-tab-pane v-if="props.task" label="小时归档" name="archives"
        ><LogArchives
          v-if="open && workspace === 'archives'"
          :task-id="props.task.id"
      /></el-tab-pane>
      <el-tab-pane v-if="props.task" label="命令记录" name="commands"
        ><CommandHistory
          v-if="open && workspace === 'commands'"
          :task-id="props.task.id"
      /></el-tab-pane>
    </el-tabs>
    <template #footer
      ><el-button @click="open = false">关闭</el-button
      ><el-button
        v-if="workspace === 'config'"
        type="primary"
        :loading="saving"
        :disabled="loading"
        @click="save"
        >保存任务</el-button
      ></template
    >
  </el-drawer>
</template>
