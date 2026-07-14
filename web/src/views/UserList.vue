<template>
  <el-card class="page-card">
    <template #header>
      <div class="page-header">
        <div class="page-title">用户列表</div>
        <div class="page-actions">
          <el-input
            v-model="query"
            placeholder="搜索账号/备注"
            clearable
            @keyup.enter="onSearch"
          />
          <el-button @click="onSearch">搜索</el-button>
          <el-button @click="resetSearch">重置</el-button>
          <el-button :loading="loading" @click="manualRefresh">刷新</el-button>
          <el-button v-if="isMobile && canWrite" :type="batchMode ? 'primary' : undefined" @click="toggleBatchMode">
            {{ batchMode ? '退出批量' : '批量' }}
          </el-button>
          <el-button v-if="canWrite" type="primary" @click="goCreateUser">添加用户</el-button>
        </div>
      </div>
    </template>

    <div v-if="!isMobile && canWrite" class="bulk-bar">
      <div class="bulk-left">
        <span class="bulk-text">已选择 {{ selectedIds.length }} 个账号</span>
        <div class="bulk-meta">
          <span class="bulk-meta-label">并发</span>
          <el-input-number v-model="batchConcurrency" :min="1" :max="maxBatchConcurrency" size="small" />
          <span class="bulk-meta-hint">越大越快，失败率也可能上升</span>
        </div>
      </div>
      <div class="bulk-right">
        <el-button
          type="success"
          :disabled="selectedIds.length === 0"
          :loading="batchRunning"
          @click="runBatch"
        >
          批量运行
        </el-button>
      </div>
    </div>

    <div v-if="isMobile && canWrite && batchMode" class="mobile-bulk-toolbar">
      <el-checkbox :model-value="allSelected" @change="toggleSelectAll">全选</el-checkbox>
      <div class="mobile-bulk-toolbar-right">
        <span class="mobile-bulk-count">已选 {{ selectedIds.length }}</span>
        <div class="mobile-bulk-concurrency">
          <span class="mobile-bulk-concurrency-label">并发</span>
          <el-input-number v-model="batchConcurrency" :min="1" :max="maxBatchConcurrency" size="small" />
        </div>
      </div>
    </div>
    
    <div v-if="isMobile" class="mobile-list" v-loading="loading">
      <el-empty v-if="!users.length && !loading" description="暂无用户" />
      <div v-else class="mobile-cards">
        <el-card v-for="u in users" :key="u.id" class="mobile-card" shadow="never">
          <div class="mobile-card-top">
            <div class="mobile-title">
              <div class="mobile-account">{{ u.phone }}</div>
              <div class="mobile-sub">ID：{{ u.id }}</div>
            </div>
            <div class="mobile-tags">
              <el-checkbox
                v-if="canWrite && batchMode"
                :model-value="isSelected(u.id)"
                @change="(val) => toggleSelect(u.id, val)"
              />
              <el-tag size="small" :type="u.last_status === 'Fail' ? 'danger' : (u.last_status === 'Success' ? 'success' : 'info')">
                {{ u.last_status || '未知' }}
              </el-tag>
              <el-tag size="small" :type="u.enable_clockin ? 'success' : 'info'">
                {{ u.enable_clockin ? '启用' : '停用' }}
              </el-tag>
            </div>
          </div>

          <div class="mobile-meta">
            <div class="mobile-meta-row">
              <div class="mobile-meta-label">最后运行</div>
              <div class="mobile-meta-value">{{ u.last_run_time || '-' }}</div>
            </div>
            <div class="mobile-meta-row" style="margin-top: 6px;">
              <div class="mobile-meta-label">备注</div>
              <div class="mobile-meta-value mobile-meta-remark">{{ u.remark || '-' }}</div>
            </div>
          </div>

          <div class="mobile-actions">
            <el-button size="small" type="success" :disabled="!canWrite" :loading="u.running" @click="runTask(u.id)" style="flex: 1;">
              运行
            </el-button>
            <el-button size="small" type="info" @click="showLogs(u)" style="flex: 1;">日志</el-button>
            <el-dropdown trigger="click" @command="(cmd) => onMobileCommand(cmd, u)">
              <el-button size="small" style="flex: 1;">更多</el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item :disabled="!canWrite" command="edit">编辑</el-dropdown-item>
                  <el-dropdown-item :disabled="!canWrite" command="remark">备注</el-dropdown-item>
                  <el-dropdown-item divided :disabled="!canDelete" command="delete">删除</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </div>
        </el-card>
      </div>
    </div>

    <div v-else class="table-wrap">
      <el-table
        :data="users"
        style="width: 100%"
        v-loading="loading"
        @selection-change="onSelectionChange"
      >
        <el-table-column v-if="canWrite" type="selection" width="44" />
        <el-table-column prop="id" label="ID" width="60" />
        <el-table-column label="账号" width="160">
          <template #default="scope">
            {{ scope.row.phone }}
          </template>
        </el-table-column>
        <el-table-column label="备注" min-width="130">
          <template #default="scope">
            <div class="desktop-remark-text" :title="scope.row.remark || '-'">
              {{ scope.row.remark || '-' }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="最后运行时间" width="180">
          <template #default="scope">
            {{ scope.row.last_run_time || '-' }}
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="scope">
            <el-tag :type="scope.row.last_status === 'Fail' ? 'danger' : (scope.row.last_status === 'Success' ? 'success' : 'info')">
              {{ scope.row.last_status || '未知' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="启用" width="80">
          <template #default="scope">
            <el-tag :type="scope.row.enable_clockin ? 'success' : 'info'">{{ scope.row.enable_clockin ? '是' : '否' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="320">
          <template #default="scope">
            <div class="desktop-actions-row">
              <el-button v-if="canWrite" size="small" @click="goEditUser(scope.row.id)">编辑</el-button>
              <el-button v-if="canWrite" size="small" type="success" @click="runTask(scope.row.id)" :loading="scope.row.running">立即运行</el-button>
              <el-button v-if="canWrite" size="small" type="warning" @click="openRemark(scope.row)">备注</el-button>
              <el-button size="small" type="info" @click="showLogs(scope.row)">日志</el-button>
              <el-button v-if="canDelete" size="small" type="danger" @click="deleteUser(scope.row.id)">删除</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div class="pager">
      <el-pagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :page-sizes="[10, 20, 50, 100, 200]"
        :total="total"
        layout="total, sizes, prev, pager, next, jumper"
        @size-change="fetchUsers"
        @current-change="fetchUsers"
      />
    </div>
    <div v-if="isMobile && canWrite && batchMode" class="mobile-batch-spacer"></div>

    <el-dialog v-model="logVisible" title="执行日志" :width="isMobile ? '92%' : '70%'">
      <div v-if="currentLogs && currentLogs.length">
         <el-collapse v-model="activeLogNames">
            <el-collapse-item v-for="(item, index) in currentLogs" :key="index" :name="index">
               <template #title>
                 <el-tag size="small" :type="item.status === 'success' ? 'success' : (item.status === 'skip' ? 'info' : 'danger')" style="margin-right: 10px;">
                    {{ item.status }}
                 </el-tag>
                 <span style="font-weight: bold;">{{ item.task_type }}</span>
                 <span style="margin-left: 10px; color: #666;">{{ item.message }}</span>
               </template>
               <div style="padding: 10px; background-color: #f5f7fa; border-radius: 4px;">
                   <div style="margin-bottom: 10px;">
                      <div style="font-weight: 700; margin-bottom: 6px;">结果</div>
                      <pre style="white-space: pre-wrap; font-family: inherit; margin: 0;">{{ item.message }}</pre>
                   </div>
                   <div v-if="item.details">
                       <el-divider>详情</el-divider>
                       <p v-for="(val, key) in item.details" :key="key">
                           <strong>{{ key }}:</strong> {{ val }}
                       </p>
                   </div>
                   <div v-if="item.report_content">
                       <el-divider>报告内容</el-divider>
                       <pre style="white-space: pre-wrap; font-family: inherit;">{{ item.report_content }}</pre>
                   </div>
                   <el-divider>原始数据</el-divider>
                   <pre style="white-space: pre-wrap; font-family: inherit; margin: 0;">{{ JSON.stringify(item, null, 2) }}</pre>
               </div>
            </el-collapse-item>
         </el-collapse>
      </div>
      <el-empty v-else description="暂无详细日志" />
      <template #footer>
        <span class="dialog-footer">
          <el-button @click="logVisible = false">关闭</el-button>
        </span>
      </template>
    </el-dialog>

    <el-dialog v-model="remarkVisible" title="编辑备注" :width="isMobile ? '92%' : '520px'">
      <el-input v-model="remarkText" type="textarea" :rows="4" placeholder="请输入备注（支持搜索）" />
      <template #footer>
        <span class="dialog-footer">
          <el-button @click="remarkVisible = false">取消</el-button>
          <el-button type="primary" :loading="remarkSaving" @click="saveRemark">保存</el-button>
        </span>
      </template>
    </el-dialog>

    <el-dialog v-model="jobVisible" title="批量任务进度" :width="isMobile ? '92%' : '720px'" @close="stopJobPolling">
      <div v-if="jobInfo" class="job-box">
        <div class="job-row">
          <div class="job-meta">任务ID：{{ jobInfo.id }}</div>
          <div class="job-meta">状态：{{ jobInfo.status }}</div>
          <div class="job-meta">并发：{{ jobInfo.concurrency }}</div>
        </div>
        <el-progress
          :percentage="jobPercent"
          :status="jobInfo.status === 'done' ? 'success' : undefined"
        />
        <div class="job-row" style="margin-top: 10px">
          <div class="job-meta">总数：{{ jobInfo.total }}</div>
          <div class="job-meta">完成：{{ jobInfo.completed }}</div>
          <div class="job-meta">成功：{{ jobInfo.success }}</div>
          <div class="job-meta">失败：{{ jobInfo.fail }}</div>
          <div class="job-meta">待重试：{{ jobInfo.failed }}</div>
        </div>

        <el-divider>最近错误</el-divider>
        <el-empty v-if="!(jobInfo.last_errors || []).length" description="暂无错误" />
        <el-table v-else :data="jobInfo.last_errors" size="small" style="width: 100%">
          <el-table-column label="时间" width="210">
            <template #default="scope">
              {{ formatBjDateTime(scope.row.ts) }}
            </template>
          </el-table-column>
          <el-table-column label="备注" width="180">
            <template #default="scope">
              {{ resolveUserRemark(scope.row.user_id) }}
            </template>
          </el-table-column>
          <el-table-column prop="message" label="原因" />
        </el-table>
      </div>
      <el-empty v-else description="暂无任务信息" />
      <template #footer>
        <span class="dialog-footer">
          <el-button
            v-if="(jobInfo?.failed || 0) > 0"
            type="warning"
            :loading="retryingFailed"
            @click="retryFailedItems"
          >
            重试失败
          </el-button>
          <el-button @click="manualRefreshJob" :loading="jobLoading">刷新</el-button>
          <el-button @click="jobVisible = false">关闭</el-button>
        </span>
      </template>
    </el-dialog>
  </el-card>

  <div v-if="isMobile && canWrite && batchMode" class="mobile-batch-actions">
    <div class="mobile-batch-actions-inner">
      <div class="mobile-batch-meta">
        <div class="mobile-batch-meta-title">批量运行</div>
        <div class="mobile-batch-meta-sub">已选 {{ selectedIds.length }} 个账号</div>
      </div>
      <el-button
        type="success"
        :disabled="selectedIds.length === 0"
        :loading="batchRunning"
        style="min-width: 120px"
        @click="runBatch"
      >
        批量运行
      </el-button>
    </div>
  </div>

  <AlipayVerificationDialog
    v-model="verificationVisible"
    :register-url="verification?.registerUrl || ''"
    :continuing="verificationContinuing"
    @continue="continueClockIn"
  />
</template>

<script setup>
import { ref, onMounted, onUnmounted, computed, h, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { http } from '../api/http'
import AlipayVerificationDialog from '../components/AlipayVerificationDialog.vue'
import { useAuthStore } from '../stores/auth'
import { notifySuccess, notifyError, notifyInfo, resolveErrorMessage } from '../utils/notify'

const users = ref([])
const loading = ref(false)
const logVisible = ref(false)
const currentLogs = ref([])
const activeLogNames = ref([])
const isMobile = ref(false)
const query = ref('')
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const selectedIds = ref([])
const batchRunning = ref(false)
const maxBatchConcurrency = ref(10)
const batchConcurrency = ref(5)
const batchMode = ref(false)
const pollTimer = ref(null)
const jobVisible = ref(false)
const jobLoading = ref(false)
const jobInfo = ref(null)
const jobId = ref(null)
const jobTimer = ref(null)
const jobPercent = ref(0)
const retryingFailed = ref(false)
const remarkVisible = ref(false)
const remarkSaving = ref(false)
const remarkText = ref('')
const remarkUserId = ref(null)
const verificationVisible = ref(false)
const verificationContinuing = ref(false)
const verification = ref(null)
const verificationUserId = ref(null)
const router = useRouter()
const auth = useAuthStore()
const canWrite = computed(() => auth.canWrite)
const canDelete = computed(() => auth.isAdmin)
let usersAbort = null
let jobAbort = null

const showInfo = (message) => {
  notifyInfo(message)
}

const flushUiMessage = async () => {
  await nextTick()
  await new Promise((resolve) => requestAnimationFrame(() => resolve()))
}

const _parseIsoLikeToDate = (ts) => {
  const s = String(ts || '').trim()
  if (!s) return null
  const normalized = s.replace(' ', 'T')
  const base = normalized.length >= 19 ? normalized.slice(0, 19) : normalized

  const hasTimezone =
    /Z$/i.test(normalized) ||
    /[+\-]\d{2}:\d{2}$/.test(normalized) ||
    /[+\-]\d{4}$/.test(normalized)

  const d = new Date(hasTimezone ? normalized : `${base}Z`)
  if (!Number.isNaN(d.getTime())) return d
  return null
}

const formatBjDateTime = (ts) => {
  if (!ts) return '-'
  const d = _parseIsoLikeToDate(ts)
  if (!d) return String(ts)
  const parts = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).formatToParts(d)
  const get = (type) => parts.find(p => p.type === type)?.value || ''
  const year = get('year')
  const month = get('month')
  const day = get('day')
  const hour = get('hour')
  const minute = get('minute')
  const second = get('second')
  if (!year || !month || !day) return String(ts)
  return `${year}年${month}月${day}日 ${hour}:${minute}:${second}`
}

const remarkMap = computed(() => {
  const m = new Map()
  for (const u of users.value || []) {
    m.set(String(u.id), String(u.remark || '').trim())
  }
  return m
})

const resolveUserRemark = (userId) => {
  const idKey = String(userId ?? '')
  const r = remarkMap.value.get(idKey)
  if (r) return r
  if (!idKey) return '-'
  return `ID：${idKey}`
}

const allSelected = computed(() => {
  const list = users.value || []
  if (!list.length) return false
  return selectedIds.value.length === list.length
})

const toggleSelectAll = (val) => {
  if (!val) {
    selectedIds.value = []
    return
  }
  selectedIds.value = (users.value || []).map(u => u.id)
}

const isSelected = (id) => selectedIds.value.includes(id)

const toggleSelect = (id, val) => {
  const v = !!val
  if (v) {
    if (!selectedIds.value.includes(id)) selectedIds.value = [...selectedIds.value, id]
    return
  }
  selectedIds.value = selectedIds.value.filter(x => x !== id)
}

const toggleBatchMode = () => {
  batchMode.value = !batchMode.value
  selectedIds.value = []
  showInfo(batchMode.value ? '已进入批量模式' : '已退出批量模式')
}

const goCreateUser = () => {
  showInfo('正在打开新增用户页面')
  router.push('/create')
}

const goEditUser = (id) => {
  showInfo('正在打开编辑页面')
  router.push(`/edit/${id}`)
}

const onMobileCommand = (cmd, user) => {
  if (!user) return
  if (cmd === 'edit') {
    if (!canWrite.value) return
    goEditUser(user.id)
    return
  }
  if (cmd === 'remark') {
    if (!canWrite.value) return
    openRemark(user)
    return
  }
  if (cmd === 'delete') {
    if (!canDelete.value) return
    deleteUser(user.id)
  }
}

const updateIsMobile = () => {
  isMobile.value = window.matchMedia('(max-width: 768px)').matches
  if (!isMobile.value) batchMode.value = false
}

const fetchUsers = async () => {
  if (usersAbort) usersAbort.abort()
  usersAbort = new AbortController()
  loading.value = true
  try {
    const res = await http.get('/users/page', {
      params: {
        page: page.value,
        pageSize: pageSize.value,
        q: query.value?.trim() || undefined,
      },
      signal: usersAbort.signal,
    })
    total.value = res.data.total || 0
    users.value = (res.data.items || []).map(u => ({ ...u, running: false }))
  } catch (error) {
    if (error?.code === 'ERR_CANCELED') return
    notifyError('获取用户列表失败')
  } finally {
    loading.value = false
  }
}

const onSearch = () => {
  page.value = 1
  fetchUsers().then(() => {
    notifySuccess(`搜索完成，共 ${total.value || 0} 条`)
  })
}

const resetSearch = () => {
  query.value = ''
  page.value = 1
  fetchUsers().then(() => {
    showInfo('已重置筛选条件')
  })
}

const manualRefresh = async () => {
  await fetchUsers()
  notifySuccess('列表已刷新')
}

const showAlipayVerification = (results, id) => {
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
  verificationUserId.value = id
  verificationVisible.value = true
  return true
}

const runTask = async (id) => {
  const user = users.value.find(u => u.id === id)
  if (!user) return
  
  user.running = true
  try {
    showInfo('正在执行任务')
    await flushUiMessage()
    const res = await http.post(`/users/${id}/run`)
    const needsVerification = showAlipayVerification(res.data?.results, id)
    if (needsVerification) {
      notifyInfo('需要完成支付宝安全验证')
    } else {
      notifySuccess('任务执行完成')
    }
    fetchUsers()
  } catch (error) {
    notifyError(`执行失败：${resolveErrorMessage(error, '请求失败')}`)
  } finally {
    user.running = false
  }
}

const continueClockIn = async () => {
  if (!verification.value || !verificationUserId.value || verificationContinuing.value) return
  verificationContinuing.value = true
  try {
    const res = await http.post(
      `/users/${verificationUserId.value}/clock-in/alipay/continue`,
      {
        out_register_no: verification.value.outRegisterNo,
        target_type: verification.value.targetType,
      }
    )
    const result = res.data?.result
    if (showAlipayVerification([result], verificationUserId.value)) {
      notifyInfo('仍需完成支付宝安全验证，验证信息已更新')
      return
    }
    if (['success', 'skip'].includes(result?.status)) {
      verificationVisible.value = false
      verification.value = null
      verificationUserId.value = null
      notifySuccess(result?.message || '打卡完成')
      await fetchUsers()
      return
    }
    notifyError(result?.message || '继续打卡失败')
  } catch (error) {
    notifyError(resolveErrorMessage(error, '继续打卡失败'))
  } finally {
    verificationContinuing.value = false
  }
}

const onSelectionChange = (rows) => {
  selectedIds.value = (rows || []).map(r => r.id)
}

const stopPolling = () => {
  if (pollTimer.value) {
    clearInterval(pollTimer.value)
    pollTimer.value = null
  }
}

const startPolling = () => {
  stopPolling()
  let ticks = 0
  pollTimer.value = setInterval(() => {
    ticks += 1
    if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return
    fetchUsers()
    if (ticks >= 40) stopPolling()
  }, 3000)
}

const runBatch = async () => {
  if (!selectedIds.value.length) return
  batchRunning.value = true
  try {
    showInfo('正在提交批量运行任务')
    await flushUiMessage()
    const res = await http.post('/users/run/batch', {
      ids: selectedIds.value,
      concurrency: Math.max(1, Math.min(Number(batchConcurrency.value || 1), Number(maxBatchConcurrency.value || 10))),
    })
    notifySuccess(`已加入队列：${res.data.queued || selectedIds.value.length} 个账号`)
    if (res.data?.job_id) {
      jobId.value = res.data.job_id
      jobVisible.value = true
      refreshJob()
      startJobPolling()
    }
    startPolling()
  } catch (error) {
    notifyError(`批量运行失败：${resolveErrorMessage(error, '请求失败')}`)
  } finally {
    batchRunning.value = false
  }
}

const refreshJob = async () => {
  if (!jobId.value) return
  if (jobAbort) jobAbort.abort()
  jobAbort = new AbortController()
  jobLoading.value = true
  try {
    const res = await http.get(`/batch-jobs/${jobId.value}`, { signal: jobAbort.signal })
    jobInfo.value = res.data
    const total = Number(res.data?.total || 0)
    const completed = Number(res.data?.completed || 0)
    jobPercent.value = total > 0 ? Math.round((completed / total) * 100) : 0
    if (res.data?.status === 'done') stopJobPolling()
  } catch (e) {
    if (e?.code === 'ERR_CANCELED') return
    notifyError(resolveErrorMessage(e, '获取任务进度失败'))
  } finally {
    jobLoading.value = false
  }
}

const manualRefreshJob = async () => {
  await refreshJob()
  showInfo('任务进度已刷新')
}

const retryFailedItems = async () => {
  if (!jobId.value || retryingFailed.value) return
  const failedCount = Number(jobInfo.value?.failed || 0)
  if (failedCount <= 0) {
    notifyInfo('当前没有可重试的失败项')
    return
  }

  try {
    await ElMessageBox.confirm(
      `将重新加入 ${failedCount} 个失败项到队列，是否继续？`,
      '重试失败项',
      {
        type: 'warning',
        confirmButtonText: '重试',
        cancelButtonText: '取消',
        closeOnClickModal: false,
      }
    )
  } catch {
    return
  }

  retryingFailed.value = true
  try {
    showInfo('正在重试失败项')
    await flushUiMessage()
    const res = await http.post(`/batch-jobs/${jobId.value}/retry-failed`)
    notifySuccess(`已重试 ${res.data?.requeued || 0} 个失败项`)
    await refreshJob()
    startJobPolling()
    startPolling()
  } catch (e) {
    notifyError(resolveErrorMessage(e, '重试失败项失败'))
  } finally {
    retryingFailed.value = false
  }
}

const stopJobPolling = () => {
  if (jobTimer.value) {
    clearInterval(jobTimer.value)
    jobTimer.value = null
  }
}

const startJobPolling = () => {
  stopJobPolling()
  jobTimer.value = setInterval(() => {
    refreshJob()
  }, 2000)
}

const deleteUser = (id) => {
  const user = users.value.find(u => u.id === id)
  const phone = user?.phone ? String(user.phone) : String(id)
  const remark = user?.remark ? String(user.remark) : ''

  ElMessageBox.confirm(
    h(
      'div',
      { class: 'danger-confirm' },
      [
        h('div', { class: 'danger-confirm__title' }, `将删除账号：${phone}`),
        remark ? h('div', { class: 'danger-confirm__meta' }, `备注：${remark}`) : null,
        h('div', { class: 'danger-confirm__desc' }, '该操作不可恢复。删除后该账号的配置与历史记录将无法在后台查看。'),
      ].filter(Boolean)
    ),
    '删除确认',
    {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      confirmButtonClass: 'el-button--danger',
      closeOnClickModal: false,
      showClose: true,
      customClass: 'danger-confirm-box',
      beforeClose: (action, instance, done) => {
        if (action !== 'confirm') {
          done()
          return
        }
        instance.confirmButtonLoading = true
        instance.confirmButtonText = '删除中...'
        http
          .delete(`/users/${id}`)
          .then(() => {
            notifySuccess('已删除')
            fetchUsers()
            done()
          })
          .catch((e) => {
            notifyError(resolveErrorMessage(e, '删除失败'))
          })
          .finally(() => {
            instance.confirmButtonLoading = false
            instance.confirmButtonText = '删除'
          })
      },
    }
  ).catch(() => {})
}

const showLogs = async (user) => {
  try {
    const res = await http.get(`/users/${user.id}/execution`)
    currentLogs.value = res.data?.results || []
    activeLogNames.value = currentLogs.value
      .map((item, index) => (item.status !== 'skip' ? index : null))
      .filter(i => i !== null)
    logVisible.value = true
    showInfo('已打开执行日志')
  } catch (error) {
    notifyError('获取日志失败')
  }
}

const openRemark = (user) => {
  remarkUserId.value = user.id
  remarkText.value = user.remark || ''
  remarkVisible.value = true
  showInfo('已打开备注编辑')
}

const saveRemark = async () => {
  if (!remarkUserId.value) return
  remarkSaving.value = true
  try {
    showInfo('正在保存备注')
    await flushUiMessage()
    await http.patch(`/users/${remarkUserId.value}`, {
      remark: remarkText.value?.trim() || null,
    })
    notifySuccess('备注已保存')
    remarkVisible.value = false
    fetchUsers()
  } catch (error) {
    notifyError(`保存备注失败：${resolveErrorMessage(error, '请求失败')}`)
  } finally {
    remarkSaving.value = false
  }
}

onMounted(fetchUsers)
onMounted(() => {
  updateIsMobile()
  window.addEventListener('resize', updateIsMobile)
})
onUnmounted(() => {
  window.removeEventListener('resize', updateIsMobile)
  stopPolling()
  stopJobPolling()
})
</script>

<style scoped>
.bulk-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 0 4px;
}
.bulk-left {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.bulk-text {
  color: #606266;
  font-size: 13px;
}
.bulk-meta {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.bulk-meta-label {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.bulk-meta-hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.mobile-bulk-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 0 4px;
}
.mobile-bulk-toolbar-right {
  display: flex;
  align-items: center;
  gap: 10px;
}
.mobile-bulk-count {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.mobile-bulk-concurrency {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.mobile-bulk-concurrency-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.pager {
  display: flex;
  justify-content: flex-end;
  padding-top: 12px;
}
.mobile-batch-spacer {
  height: calc(72px + env(safe-area-inset-bottom));
}
.mobile-batch-actions {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  border-top: 1px solid var(--el-border-color);
  background-color: var(--el-bg-color);
  padding: 10px calc(12px + env(safe-area-inset-left)) calc(10px + env(safe-area-inset-bottom)) calc(12px + env(safe-area-inset-right));
  z-index: 2000;
}
.mobile-batch-actions-inner {
  max-width: 1200px;
  margin: 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.mobile-batch-meta {
  min-width: 0;
}
.mobile-batch-meta-title {
  font-weight: 700;
  font-size: 14px;
  color: var(--el-text-color-primary);
}
.mobile-batch-meta-sub {
  margin-top: 2px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.mobile-cards {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.mobile-card {
  border: 1px solid var(--el-border-color-lighter);
  background-color: var(--el-bg-color);
}
.mobile-card-top {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}
.mobile-account {
  font-size: 16px;
  font-weight: 600;
}
.mobile-sub {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  margin-top: 2px;
}
.mobile-tags {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
  align-items: flex-start;
}
.mobile-meta {
  padding: 8px 10px;
  background-color: var(--el-fill-color-light);
  border-radius: 8px;
  margin-bottom: 10px;
}
.mobile-meta-row {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  font-size: 12px;
}
.mobile-meta-label {
  color: var(--el-text-color-secondary);
}
.mobile-meta-value {
  color: var(--el-text-color-regular);
  text-align: right;
  word-break: break-all;
}
.mobile-meta-remark {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  word-break: break-word;
}
.mobile-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.desktop-remark-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.desktop-actions-row {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: nowrap;
  white-space: nowrap;
}
.desktop-actions-row :deep(.el-button) {
  margin-left: 0;
}
.job-box {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.job-row {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}
.job-meta {
  font-size: 13px;
  color: var(--el-text-color-regular);
}
:global(.danger-confirm-box.el-message-box) {
  border-radius: 12px;
}
:global(.danger-confirm-box .danger-confirm__title) {
  font-weight: 700;
  color: var(--el-text-color-primary);
}
:global(.danger-confirm-box .danger-confirm__meta) {
  margin-top: 6px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  word-break: break-word;
}
:global(.danger-confirm-box .danger-confirm__desc) {
  margin-top: 10px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}
</style>
