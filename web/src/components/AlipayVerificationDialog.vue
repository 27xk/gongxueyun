<script setup>
import { computed } from 'vue'

const visible = defineModel({ type: Boolean, default: false })
const props = defineProps({
  registerUrl: { type: String, default: '' },
  continuing: { type: Boolean, default: false },
})
const emit = defineEmits(['continue'])

const safeRegisterUrl = computed(() => {
  const value = String(props.registerUrl || '').trim()
  return value.startsWith('alipays://') ? value : ''
})

const openAlipay = () => {
  if (!safeRegisterUrl.value || props.continuing) return
  window.location.assign(safeRegisterUrl.value)
}

const beforeClose = (done) => {
  if (!props.continuing) done()
}
</script>

<template>
  <el-dialog
    v-model="visible"
    title="支付宝安全验证"
    width="min(92vw, 480px)"
    :before-close="beforeClose"
    :close-on-click-modal="!continuing"
    :close-on-press-escape="!continuing"
    :show-close="!continuing"
    append-to-body
  >
    <div class="verification-content">
      <div class="verification-status">安全验证待完成</div>
      <div class="verification-message">请在支付宝完成验证后继续本次打卡。</div>
      <el-alert
        v-if="!safeRegisterUrl"
        title="验证链接不可用，请关闭后重新发起打卡"
        type="error"
        :closable="false"
        show-icon
      />
    </div>

    <template #footer>
      <div class="verification-actions">
        <el-button :disabled="!safeRegisterUrl || continuing" @click="openAlipay">
          前往支付宝验证
        </el-button>
        <el-button type="primary" :loading="continuing" @click="emit('continue')">
          验证完成，继续打卡
        </el-button>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.verification-content {
  display: grid;
  gap: 12px;
}

.verification-status {
  font-size: 16px;
  font-weight: 700;
  color: var(--el-text-color-primary);
}

.verification-message {
  color: var(--el-text-color-regular);
  line-height: 1.6;
}

.verification-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  flex-wrap: wrap;
}

.verification-actions :deep(.el-button) {
  margin-left: 0;
}

@media (max-width: 520px) {
  .verification-actions {
    display: grid;
    grid-template-columns: 1fr;
  }

  .verification-actions :deep(.el-button) {
    width: 100%;
  }
}
</style>
