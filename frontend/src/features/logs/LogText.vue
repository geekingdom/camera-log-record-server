<script setup lang="ts">
// 安全日志文本：只创建 Vue span 节点，设备输出永远不进入 v-html。
import { computed } from "vue";
import { tokenizeLogText } from "./logText";

const props = defineProps<{ text: string; query?: string; activeStart?: number }>();
const tokens = computed(() => tokenizeLogText(props.text, props.query, props.activeStart));
const rendered = computed(() => {
  let searchOffset = 0;
  return tokens.value.map((token) => {
    const start = searchOffset;
    searchOffset += token.text.length;
    return { ...token, active: token.highlighted && props.activeStart === start };
  });
});
</script>

<template>
  <span
    v-for="(token, index) in rendered"
    :key="index"
    :class="[token.tone && `log-level-${token.tone}`, { 'log-search-hit': token.highlighted, 'log-search-active': token.active }]"
  >{{ token.text }}</span>
</template>

<style scoped>
.log-level-error { color: #ff7777; font-weight: 700; }
.log-level-warning { color: #f2c76e; font-weight: 700; }
.log-level-info { color: #8cc8ff; }
.log-level-debug { color: #b8a7e8; }
.log-level-trace { color: #8b9c96; }
.log-search-hit { background: #765f18; color: #fff7d1; }
.log-search-active { background: #e3a929; color: #16120a; outline: 1px solid #ffe39b; }
</style>
