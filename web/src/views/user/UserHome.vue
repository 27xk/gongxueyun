<template>
  <div class="user-home">
    <el-card class="page-card" shadow="never">
      <template #header>
        <div class="panel-header">
          <div>
            <div class="panel-title">用户工作台</div>
            <div class="panel-subtitle">统一操作现有 /app/* 能力</div>
          </div>
          <div class="panel-actions">
            <el-button :loading="loading" @click="loadAll">刷新</el-button>
            <el-button plain @click="router.push('/u/settings')">去设置</el-button>
          </div>
        </div>
      </template>

      <div class="summary-grid">
        <div class="summary-item">
          <div class="summary-label">登录账号</div>
          <div class="summary-value">{{ auth.phone || '-' }}</div>
        </div>
        <div class="summary-item">
          <div class="summary-label">绑定状态</div>
          <div class="summary-value">{{ me.bound ? '已绑定工学云账号' : '未绑定' }}</div>
        </div>
        <div class="summary-item">
          <div class="summary-label">最近状态</div>
          <div class="summary-value">{{ latestStatus }}</div>
        </div>
      </div>
    </el-card>

    <el-card class="page-card" shadow="never">
      <template #header>
        <div class="panel-header">
          <div>
            <div class="panel-title">执行任务</div>
            <div class="panel-subtitle">调用 /app/run 并刷新 /app/execution</div>
          </div>
        </div>
      </template>

      <div class="run-actions">
        <el-button type="primary" :loading="runLoading === 'all'" :disabled="!me.bound" @click="runTask('')">执行全部</el-button>
        <el-button :loading="runLoading === 'clock_in'" :disabled="!me.bound" @click="runTask('clock_in')">仅打卡</el-button>
        <el-button :loading="runLoading === 'daily_report'" :disabled="!me.bound" @click="runTask('daily_report')">仅日报</el-button>
        <el-button :loading="runLoading === 'report'" :disabled="!me.bound" @click="runTask('report')">全部报告</el-button>
      </div>
      <div v-if="!me.bound" class="inline-hint">尚未绑定工学云账号，请先到设置页完成绑定。</div>
    </el-card>

    <el-card class="page-card" shadow="never">
      <template #header>
        <div class="panel-header">
          <div>
            <div class="panel-title">最近执行记录</div>
            <div class="panel-subtitle">来自 /app/execution</div>
          </div>
        </div>
      </template>

      <el-empty v-if="!executions.length" description="暂无执行记录" />
      <div v-else class="execution-list">
        <div v-for="(item, index) in executions" :key="index" class="execution-item">
          <div class="execution-head">
            <strong>{{ item.task_type || '未知任务' }}</strong>
            <el-tag size="small" :type="item.status === 'fail' ? 'danger' : (item.status === 'success' ? 'success' : 'info')">
              {{ item.status || 'unknown' }}
            </el-tag>
          </div>
          <div class="execution-message">{{ item.message || '-' }}</div>
        </div>
      </div>
    </el-card>

    <el-card class="page-card" shadow="never">
      <template #header>
        <div class="panel-header">
          <div>
            <div class="panel-title">日报</div>
            <div class="panel-subtitle">生成 / 提交日报</div>
          </div>
        </div>
      </template>

      <div class="report-actions">
        <el-button type="primary" :loading="generating" :disabled="!me.bound" @click="generateDailyReport">生成日报</el-button>
        <el-button type="success" :loading="submitting" :disabled="!me.bound || !dailyContent.trim()" @click="submitDailyReport">提交日报</el-button>
      </div>
      <el-input
        v-model="dailyContent"
        type="textarea"
        :rows="8"
        placeholder="先点击生成日报，或手动输入内容后提交"
      />
    </el-card>

    <AlipayVerificationDialog
      v-model="verificationVisible"
      :register-url="verification?.registerUrl || ''"
      :continuing="verificationContinuing"
      @continue="continueClockIn"
    />
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { userHttp } from '../../api/userHttp'
import AlipayVerificationDialog from '../../components/AlipayVerificationDialog.vue'
import { useUserAuthStore } from '../../stores/userAuth'
import { notifyError, notifyInfo, notifySuccess, resolveErrorMessage } from '../../utils/notify'

const router = useRouter()
const auth = useUserAuthStore()

const loading = ref(false)
const generating = ref(false)
const submitting = ref(false)
const runLoading = ref('')
const me = ref({ app_phone: '', bound: false, task_user: null })
const executions = ref([])
const dailyContent = ref('')
const verificationVisible = ref(false)
const verificationContinuing = ref(false)
const verification = ref(null)

const latestStatus = computed(() => {
  if (!executions.value.length) return '-'
  const item = executions.value.find((entry) => entry?.status !== 'skip') || executions.value[0]
  return item?.status || '-'
})

const loadMe = async () => {
  const res = await userHttp.get('/app/me')
  me.value = {
    app_phone: res.data?.app_phone || auth.phone,
    bound: !!res.data?.bound,
    task_user: res.data?.task_user || null,
  }
}

const loadExecution = async () => {
  const res = await userHttp.get('/app/execution')
  executions.value = Array.isArray(res.data?.results) ? res.data.results : []
}

const loadAll = async () => {
  loading.value = true
  try {
    await loadMe()
    if (me.value.bound) {
      await loadExecution()
    } else {
      executions.value = []
    }
  } catch (e) {
    notifyError(resolveErrorMessage(e, '加载用户工作台失败'))
  } finally {
    loading.value = false
  }
}

const showAlipayVerification = (results) => {
  const item = (Array.isArray(results) ? results : []).find((entry) => {
    const details = entry?.details
    return (
      entry?.status === 'fail' &&
      details?.outRegisterNo &&
      String(details?.registerUrl || '').startsWith('alipays://')
    )
  })
  if (!item) return false

  const targetType = String(item.details?.target_type || 'START').toUpperCase()
  verification.value = {
    outRegisterNo: String(item.details.outRegisterNo),
    registerUrl: String(item.details.registerUrl),
    targetType: ['START', 'END'].includes(targetType) ? targetType : 'START',
  }
  verificationVisible.value = true
  return true
}

const runTask = async (taskType) => {
  runLoading.value = taskType || 'all'
  try {
    const res = await userHttp.post('/app/run', taskType ? { task_type: taskType } : {})
    const needsVerification = showAlipayVerification(res.data?.results)
    if (needsVerification) {
      notifyInfo('需要完成支付宝安全验证')
    } else {
      notifySuccess('已触发执行')
    }
    await loadExecution()
    await loadMe()
  } catch (e) {
    notifyError(resolveErrorMessage(e, '执行失败'))
  } finally {
    runLoading.value = ''
  }
}

const continueClockIn = async () => {
  if (!verification.value || verificationContinuing.value) return
  verificationContinuing.value = true
  try {
    const res = await userHttp.post('/app/clock-in/alipay/continue', {
      out_register_no: verification.value.outRegisterNo,
      target_type: verification.value.targetType,
    })
    const result = res.data?.result
    if (showAlipayVerification([result])) {
      notifyInfo('仍需完成支付宝安全验证，验证信息已更新')
      return
    }
    if (['success', 'skip'].includes(result?.status)) {
      verificationVisible.value = false
      verification.value = null
      notifySuccess(result?.message || '打卡完成')
      await loadExecution()
      await loadMe()
      return
    }
    notifyError(result?.message || '继续打卡失败')
  } catch (e) {
    notifyError(resolveErrorMessage(e, '继续打卡失败'))
  } finally {
    verificationContinuing.value = false
  }
}

const generateDailyReport = async () => {
  generating.value = true
  try {
    const res = await userHttp.post('/app/reports/daily/generate')
    dailyContent.value = String(res.data?.content || '')
    notifySuccess('日报已生成')
  } catch (e) {
    notifyError(resolveErrorMessage(e, '生成日报失败'))
  } finally {
    generating.value = false
  }
}

const submitDailyReport = async () => {
  submitting.value = true
  try {
    await userHttp.post('/app/reports/daily/submit', { content: dailyContent.value })
    notifySuccess('日报已提交')
    await loadExecution()
  } catch (e) {
    notifyError(resolveErrorMessage(e, '提交日报失败'))
  } finally {
    submitting.value = false
  }
}

loadAll()
</script>

<style scoped>
.user-home {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.panel-title {
  font-size: 16px;
  font-weight: 700;
}
.panel-subtitle {
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.panel-actions,
.run-actions,
.report-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.summary-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}
.summary-item {
  padding: 14px;
  border-radius: 12px;
  background: var(--el-fill-color-light);
}
.summary-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.summary-value {
  margin-top: 8px;
  font-size: 15px;
  font-weight: 600;
}
.inline-hint {
  margin-top: 10px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.execution-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.execution-item {
  padding: 12px 14px;
  border: 1px solid var(--el-border-color-light);
  border-radius: 12px;
}
.execution-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
}
.execution-message {
  margin-top: 8px;
  color: var(--el-text-color-regular);
  line-height: 1.6;
}
@media (max-width: 768px) {
  .summary-grid {
    grid-template-columns: 1fr;
  }
}
</style>
