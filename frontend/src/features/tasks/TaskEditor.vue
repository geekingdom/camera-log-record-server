<script setup lang="ts">
// 任务编辑器管理版本化配置保存和工作区切换；日志、归档、命令记录保持为独立功能模块。
import { computed, ref, watch } from "vue";
import { ElMessage, ElMessageBox, type FormInstance } from "element-plus";
import { api } from "../../shared/api";
import { revealResourceCredential } from "../resources/resourceCredentialApi";
import { confirmAction } from "../../shared/confirm";
import { usePermissions } from "../../shared/permissions";
import type { Resource, Task, Template } from "../../shared/types";
import { normalizeSshTarget, sshTargetOptions } from "./sshTarget";
import CommandEditor from "../commands/CommandEditor.vue";
import LiveLogs from "../logs/LiveLogs.vue";
import LogArchives from "../logs/LogArchives.vue";
import CommandHistory from "../commands/CommandHistory.vue";
const open = defineModel<boolean>({ required: true });
const props = defineProps<{ task?: Task; templates: Template[]; initialWorkspace?: string; initialResource?: Resource; canEdit?: boolean; userId?: string }>();
const emit = defineEmits<{ saved: [] }>();
const permissions = usePermissions();
const canSave = computed(() => (!props.task || props.canEdit === true) && permissions.can(props.task ? "tasks:write" : "tasks:create"));
const blank = (): Task => ({
  id: "",
  name: "",
  protocol: "SSH",
  sshTarget: "HOST",
  ip: "",
  port: 22,
  username: "",
  password: "",
  useResourceCredentials: true,
  initialCommands: [],
  scheduledCommands: [],
  resourceId: "",
  serialServerResourceId: null,
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
  savingTemplate = ref(false),
  saving = ref(false),
  loading = ref(false);
const resources = ref<Resource[]>([]), resourceLoading = ref(false);
const serialServerMode = ref<"custom" | "resource">("custom");
const templatePageSize = 20;
const portTouched = ref(false),
  workspace = ref("config"),
  serial = computed(() => form.value.protocol === "TELNET_SERIAL");
const linkedResource = computed(() => resources.value.find((item) => item.id === form.value.resourceId));
const linkedSerialServer = computed(() => resources.value.find((item) => item.id === form.value.serialServerResourceId));
const serialUsesResource = computed(() => serial.value && Boolean(form.value.serialServerResourceId));
const serialServerOwnedTask = computed(() => linkedResource.value?.kind === "SERIAL_SERVER" ||
  (!props.task && props.initialResource?.id === form.value.resourceId && props.initialResource?.kind === "SERIAL_SERVER"));
const networkResourceUnavailable = computed(() => linkedResource.value?.kind === "HIKVISION_NETWORK" &&
  linkedResource.value.healthStatus !== "ONLINE");
let generation = 0;
let templateGeneration = 0;
let resourceGeneration = 0;
let credentialGeneration = 0;
const credentialLoading = ref(false);
const usingSavedResourceCredentials = computed(() => !serial.value && form.value.useResourceCredentials !== false && Boolean(linkedResource.value));
// 模板选择器独立分页；序号保证抽屉关闭、重开或翻页时旧响应不会覆盖当前页。
async function loadTemplatePage(page: number) {
  if (!permissions.can("templates:read")) return;
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
async function loadResources() {
  if (!permissions.can("tasks:read")) return;
  const current = ++resourceGeneration;
  resourceLoading.value = true;
  try {
    const items: Resource[] = [];
    let page = 1;
    let total = 0;
    do {
      const response = await api.resources(page, 100);
      items.push(...response.items); total = response.total; page += 1;
      if (!response.items.length) break;
    } while (items.length < total && current === resourceGeneration);
    if (current === resourceGeneration && open.value) resources.value = items;
  } catch (error) {
    if (current === resourceGeneration && open.value) ElMessage.error(error instanceof Error ? error.message : "读取设备资源失败");
  } finally {
    if (current === resourceGeneration) resourceLoading.value = false;
  }
}
async function applyResourceCredential(resource: Resource | undefined) {
  const current = ++credentialGeneration;
  if (!open.value || props.task || !resource || resource.kind !== "HIKVISION_NETWORK" || serial.value) return;
  form.value.useResourceCredentials = true;
  form.value.username = resource.username ?? "";
  form.value.password = "";
  if (!permissions.can("resources:write")) return;
  credentialLoading.value = true;
  try {
    const credential = await revealResourceCredential(resource.id);
    if (current !== credentialGeneration || props.task || form.value.resourceId !== resource.id || !form.value.useResourceCredentials) return;
    form.value.username = credential.username;
    form.value.password = credential.password;
  } catch {
    // 非创建者可继续由服务端复用密文；不向其暴露资源所有权或凭据读取失败细节。
    if (current === credentialGeneration && form.value.resourceId === resource.id && form.value.useResourceCredentials)
      form.value.password = "";
  } finally {
    if (current === credentialGeneration) credentialLoading.value = false;
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
      required: !serial.value && !props.task && !usingSavedResourceCredentials.value,
      message: "此协议需要密码",
      trigger: "blur",
    },
  ],
  resourceId: [{ required: !props.task, message: "请选择设备资源", trigger: "change" }],
  serialServerResourceId: [{ required: serial.value && !serialServerOwnedTask.value &&
    serialServerMode.value === "resource", message: "请选择串口服务器", trigger: "change" }],
}));
// generation 防止快速切换任务时较慢的详情请求覆盖当前编辑表单。
watch(
  () => [open.value, props.task] as const,
  async ([visible, task]) => {
    const current = ++generation;
    ++templateGeneration;
    ++credentialGeneration;
    if (!visible) {
      templateLoading.value = false;
      savingTemplate.value = false;
      ++resourceGeneration;
      resourceLoading.value = false;
      credentialLoading.value = false;
      return;
    }
    if (!canSave.value || (task && !permissions.can("tasks:read"))) { open.value = false; return; }
    loading.value = true;
    saving.value = false;
    resources.value = props.initialResource ? [props.initialResource] : [];
    workspace.value = props.initialWorkspace ?? "config";
    clearPassword.value = false;
    templateId.value = "";
    templateItems.value = [];
    templatePage.value = 1;
    templateTotal.value = 0;
    void loadTemplatePage(1);
    void loadResources();
    autoStart.value = false;
    serialServerMode.value = task?.serialServerResourceId ? "resource" : "custom";
    portTouched.value = Boolean(task);
    try {
      const loaded: Task = task ? await api.task(task.id) : {
        ...blank(), resourceId: props.initialResource?.id ?? "",
        protocol: props.initialResource?.kind === "SERIAL_SERVER" ? "TELNET_SERIAL" as const : "SSH" as const,
        ip: props.initialResource?.ip ?? "",
        port: props.initialResource?.kind === "SERIAL_SERVER" ? undefined : 22,
      };
      if (current !== generation) return;
      loaded.password = "";
      loaded.sshTarget = normalizeSshTarget(loaded.protocol, loaded.sshTarget);
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
    form.value.sshTarget = normalizeSshTarget(protocol, form.value.sshTarget);
  },
);
watch([linkedResource, serial], ([resource]) => {
  if (resource?.kind === "SERIAL_SERVER") {
    form.value.protocol = "TELNET_SERIAL";
    form.value.serialServerResourceId = null;
  }
  if (!resource || (serial.value && resource.kind === "HIKVISION_NETWORK")) return;
  form.value.ip = resource.ip;
}, { immediate: true });
// 仅资源选择本身变化时预填，资源列表刷新会替换对象引用，不能覆盖用户已编辑的凭据。
watch(
  () => [linkedResource.value?.id, serial.value] as const,
  ([resourceId]) => { void applyResourceCredential(resources.value.find(item => item.id === resourceId)); },
);
watch(linkedSerialServer, (resource) => {
  if (!resource || !serial.value) return;
  form.value.ip = resource.ip;
});
watch(serial, (isSerial) => {
  if (!isSerial) form.value.serialServerResourceId = null;
});
watch(serialServerMode, (mode) => {
  if (mode === "custom") form.value.serialServerResourceId = null;
});
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
async function quickSaveTemplate() {
  if (!permissions.can("templates:write") || templateId.value || saving.value || savingTemplate.value
      || !editorRef.value?.validate()) return;
  const current = generation;
  const commands = JSON.stringify([form.value.initialCommands, form.value.scheduledCommands]);
  savingTemplate.value = true;
  try {
    const result = await ElMessageBox.prompt("模板默认仅自己可见。", "保存为命令模板", {
      inputPlaceholder: "模板名称", inputValidator: value => !value.trim() ? "请输入模板名称"
        : value.trim().length > 128 ? "模板名称不能超过 128 个字符" : true,
      confirmButtonText: "保存", cancelButtonText: "取消",
    });
    const name = result.value.trim();
    if (current !== generation || !open.value || commands !== JSON.stringify([form.value.initialCommands, form.value.scheduledCommands])) return;
    const [initialCommands, scheduledCommands] = JSON.parse(commands);
    const template = await api.createTemplate({ name, description: "", initialCommands,
      scheduledCommands, sharedWith: [], sharedWithAll: false });
    if (current !== generation || !open.value || commands !== JSON.stringify([form.value.initialCommands, form.value.scheduledCommands])) return;
    await loadTemplatePage(1);
    if (current !== generation || !open.value || commands !== JSON.stringify([form.value.initialCommands, form.value.scheduledCommands])) return;
    // 翻页响应可能尚未包含刚创建的模板；刷新后再插入，确保可立即选择。
    templateItems.value = [template, ...templateItems.value.filter(item => item.id !== template.id)];
    templateId.value = template.id;
    form.value.sourceTemplateId = template.id;
    form.value.sourceTemplateVersion = template.version;
    ElMessage.success("命令模板已保存");
  } catch (error) {
    if (error !== "cancel" && error !== "close") ElMessage.error(error instanceof Error ? error.message : "保存命令模板失败");
  } finally {
    if (current === generation) savingTemplate.value = false;
  }
}
// 仅提交与初始快照不同的字段，保留编辑密码为空时“不覆盖原密码”的后端语义。
async function save() {
  if (!canSave.value) return;
  if (autoStart.value && !permissions.can("tasks:control")) return;
  const current = generation;
  if (
    saving.value ||
    !editorRef.value?.validate() ||
    !(await formRef.value?.validate().catch(() => false))
  )
    return;
  if (current !== generation || !open.value || saving.value) return;
  saving.value = true;
  try {
    const payload = JSON.parse(JSON.stringify(form.value)) as Task;
    if (serialUsesResource.value && linkedSerialServer.value) payload.ip = linkedSerialServer.value.ip;
    else if ((!serial.value || serialServerOwnedTask.value) && linkedResource.value) payload.ip = linkedResource.value.ip;
    if (props.task) {
      const changes = Object.fromEntries(
        Object.entries(payload).filter(
          ([key, value]) =>
            key !== "resourceId" &&
            key !== "useResourceCredentials" &&
            JSON.stringify(value) !==
            JSON.stringify(original.value[key as keyof Task]),
        ),
      );
      if (clearPassword.value) changes.clearPassword = true;
      const restarting = props.task.desiredState === "RUNNING" &&
        Object.keys(changes).some(
          (key) => !["name", "description"].includes(key),
        );
      if (!(await confirmAction(restarting
        ? `确认修改任务“${payload.name}”？修改连接或命令将重新开启任务，定时命令次数从零计算。`
        : `确认保存任务“${payload.name}”的修改？`, "确认编辑任务"))) return;
      if (current !== generation || !open.value) return;
      await api.updateTask(props.task.id, {
        ...changes,
        version: payload.version ?? 1,
      });
    } else await api.createTask(payload, autoStart.value);
    emit("saved");
    if (current === generation && open.value) {
      ElMessage.success("任务已保存");
      open.value = false;
    }
  } catch (error) {
    if (current === generation && open.value)
      ElMessage.error(error instanceof Error ? error.message : "保存失败");
  } finally {
    if (current === generation) saving.value = false;
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
          :disabled="saving"
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
                  ><el-select v-model="form.protocol" :disabled="serialServerOwnedTask"
                  ><el-option label="SSH" value="SSH" :disabled="networkResourceUnavailable" /><el-option
                    label="Telnet 设备"
                    value="TELNET_DEVICE" :disabled="networkResourceUnavailable" /><el-option
                    label="Telnet 串口"
                    value="TELNET_SERIAL" /></el-select
              ></el-form-item>
              <el-form-item v-if="form.protocol === 'SSH'" label="日志采集任务类型">
                <el-select v-model="form.sshTarget">
                  <el-option v-for="option in sshTargetOptions" :key="option.value" :label="option.label" :value="option.value" />
                </el-select>
              </el-form-item>
              <el-form-item v-if="!serial" label="设备资源" prop="resourceId">
                <el-select v-model="form.resourceId" :loading="resourceLoading" :disabled="Boolean(props.task?.resourceId)" placeholder="选择已认证的海康设备">
                  <el-option v-for="item in resources.filter(resource => resource.kind === 'HIKVISION_NETWORK')" :key="item.id" :label="`${item.name} · ${item.ip}`" :value="item.id" />
                </el-select>
                <small v-if="props.task?.resourceId" class="inline-option">已关联任务不能转移资源</small>
                <small v-else-if="networkResourceUnavailable" class="inline-option">当前认证未成功，只能创建 Telnet 串口任务</small>
              </el-form-item>
              <template v-else-if="serial">
                <el-form-item label="关联设备资源" prop="resourceId">
                  <el-select v-model="form.resourceId" :loading="resourceLoading" :disabled="Boolean(props.task?.resourceId)" placeholder="选择海康网络设备">
                    <el-option v-for="item in resources" :key="item.id" :label="`${item.name} · ${item.ip}`" :value="item.id" />
                  </el-select>
                </el-form-item>
                <el-form-item v-if="!serialServerOwnedTask" label="串口服务器来源">
                  <el-radio-group v-model="serialServerMode">
                    <el-radio value="custom">自定义地址</el-radio><el-radio value="resource">已有串口服务器</el-radio>
                  </el-radio-group>
                </el-form-item>
                <el-form-item v-if="!serialServerOwnedTask && serialServerMode === 'resource'" label="串口服务器资源" prop="serialServerResourceId">
                  <el-select v-model="form.serialServerResourceId" :loading="resourceLoading" placeholder="选择串口服务器">
                    <el-option v-for="item in resources.filter(resource => resource.kind === 'SERIAL_SERVER')" :key="item.id" :label="`${item.name} · ${item.ip}`" :value="item.id" />
                  </el-select>
                </el-form-item>
              </template>
              <el-form-item
                :label="serial ? '串口服务器 IP' : '设备 IP'"
                prop="ip"
                ><el-input v-model="form.ip" :disabled="serialServerOwnedTask || (!serial && Boolean(form.resourceId)) || serialUsesResource"
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
                ><el-input v-model="form.username" autocomplete="off" @input="form.useResourceCredentials = false"
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
                  :placeholder="usingSavedResourceCredentials ? '使用资源已保存的密码' : ''"
                  :loading="credentialLoading"
                  @input="form.useResourceCredentials = false"
              /></el-form-item>
            </div>
            <el-checkbox v-if="serial && props.task" v-model="clearPassword">清除已保存密码</el-checkbox>
            <el-checkbox v-if="!props.task && permissions.can('tasks:control')" v-model="autoStart"
              >保存后立即启动</el-checkbox
            >
          </section>
          <section class="form-section">
            <div class="section-heading">
              <h2>命令配置</h2>
              <el-button v-if="!templateId && permissions.can('templates:write')" text type="primary" :loading="savingTemplate" :disabled="savingTemplate || (!form.initialCommands.length && !form.scheduledCommands.length)" @click="quickSaveTemplate">另存为模板</el-button>
              <el-select
                v-if="permissions.can('templates:read')"
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
      <el-tab-pane v-if="props.task && permissions.can('logs:read')" label="实时打印" name="live"
        ><LiveLogs v-if="open && workspace === 'live'" :task-id="props.task.id" :can-send="props.canEdit && permissions.can('commands:send')" :user-id="props.userId" @history="workspace = 'archives'"
      /></el-tab-pane>
      <el-tab-pane v-if="props.task && permissions.can('logs:read')" label="小时归档" name="archives"
        ><LogArchives
          v-if="open && workspace === 'archives'"
          :task-id="props.task.id"
          :can-download="permissions.can('logs:download')"
      /></el-tab-pane>
      <el-tab-pane v-if="props.task && permissions.can('tasks:read')" label="命令记录" name="commands"
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
        :disabled="loading || resourceLoading"
        @click="save"
        >保存任务</el-button
      ></template
    >
  </el-drawer>
</template>
