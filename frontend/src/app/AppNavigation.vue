<script setup lang="ts">
// 应用壳层只展示导航和会话入口；领域页签、权限过滤及数据加载仍由 App 装配。
import type { Component } from "vue";
import {
  BookOpen,
  ChevronRight,
  KeyRound,
  LogOut,
  PanelLeftClose,
  PanelLeftOpen,
  Terminal,
} from "lucide-vue-next";

defineProps<{
  authenticated: boolean;
  items: { key: string; label: string; icon: Component }[];
  activeTab: string;
  sidebarCollapsed: boolean;
  displayName?: string;
  lastUpdated: string;
}>();
const emit = defineEmits<{
  navigate: [key: string];
  toggleSidebar: [];
  openPassword: [];
  logout: [];
}>();
</script>

<template>
  <aside v-if="authenticated" class="sidebar">
    <div class="sidebar-brand">
      <span class="brand-mark"><Terminal :size="23" /></span>
      <div><strong>设备日志服务</strong><small>LOG RECORD</small></div>
    </div>
    <div class="nav-caption">工作空间</div>
    <nav role="tablist" aria-label="工作空间导航" class="side-nav">
      <button
        v-for="item in items"
        :key="item.key"
        role="tab"
        :aria-label="item.label"
        :title="sidebarCollapsed ? item.label : undefined"
        :aria-selected="activeTab === item.key"
        :class="{ active: activeTab === item.key }"
        @click="emit('navigate', item.key)"
      >
        <component :is="item.icon" :size="18" /><span>{{ item.label }}</span
        ><ChevronRight v-if="activeTab === item.key" :size="14" />
      </button>
    </nav>
    <div class="sidebar-footer">
      <a
        href="https://github.com/geekingdom/camera-log-record-server/blob/main/docs/api.md"
        target="_blank"
        rel="noopener"
        ><BookOpen :size="16" /> API 文档</a
      ><span><i /> 已连接控制台</span>
    </div>
  </aside>
  <div class="main-column">
    <header class="topbar">
      <div v-if="authenticated" class="breadcrumb">
        <el-tooltip :content="sidebarCollapsed ? '展开导航栏' : '折叠导航栏'"
          ><el-button
            text
            :icon="sidebarCollapsed ? PanelLeftOpen : PanelLeftClose"
            :aria-label="sidebarCollapsed ? '展开导航栏' : '折叠导航栏'"
            :aria-expanded="!sidebarCollapsed"
            @click="emit('toggleSidebar')" /></el-tooltip
        ><span>工作空间</span><ChevronRight :size="14" /><strong>{{
          items.find((item) => item.key === activeTab)?.label
        }}</strong>
      </div>
      <div v-else class="brand">
        <Terminal :size="22" /><span>设备日志服务</span>
      </div>
      <div v-if="authenticated" class="topbar-actions">
        <span class="refresh-time">{{ displayName }}</span
        ><span v-if="lastUpdated" class="refresh-time"
          >任务更新 {{ lastUpdated }}</span
        ><el-tooltip content="修改密码"
          ><el-button
            text
            :icon="KeyRound"
            aria-label="修改密码"
            @click="emit('openPassword')" /></el-tooltip
        ><el-tooltip content="退出控制台"
          ><el-button
            text
            :icon="LogOut"
            aria-label="退出"
            @click="emit('logout')"
        /></el-tooltip>
      </div>
    </header>
    <slot />
  </div>
</template>
