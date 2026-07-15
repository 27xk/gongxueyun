<script setup>
import { computed, onMounted, reactive, shallowRef } from 'vue'
import { http } from '../api/http'
import { userHttp } from '../api/userHttp'
import { notifyError, notifySuccess, resolveErrorMessage } from '../utils/notify'
import ClockInPreauthorizationDialog from './ClockInPreauthorizationDialog.vue'
import ClockInPreauthorizationList from './ClockInPreauthorizationList.vue'

const props = defineProps({
  mode: { type: String, required: true },
  userId: { type: Number, default: null },
})

const PAGE_SIZE = 20

const createListState = () => reactive({
  items: [],
  summary: {
    pending: 0,
    authorized: 0,
    consumed: 0,
    reauthorize_required: 0,
  },
  page: 1,
  pageSize: PAGE_SIZE,
  total: 0,
  status: '',
  targetType: '',
  loading: false,
  loaded: false,
})

const future = createListState()
const past = createListState()
const metadata = reactive({
  account: '',
  addedDate: '',
  planEndDate: '',
  startTime: '',
  endTime: '',
})

const dialogVisible = shallowRef(false)
const completing = shallowRef(false)
const startingKey = shallowRef('')
const pastExpanded = shallowRef(false)
const authorizationSession = shallowRef(null)
const selectedTarget = shallowRef(null)

const apiClient = computed(() => (props.mode === 'user' ? userHttp : http))
const apiBase = computed(() => {
  if (props.mode === 'user') return '/app/clock-in/preauthorizations'
  return `/users/${props.userId}/clock-in/preauthorizations`
})

const futureSummary = computed(() => [
  { key: 'pending', label: '待授权', value: future.summary.pending },
  { key: 'authorized', label: '已授权', value: future.summary.authorized },
  { key: 'consumed', label: '已使用', value: future.summary.consumed },
  { key: 'reauthorize_required', label: '需重新授权', value: future.summary.reauthorize_required },
])

const updateState = (state, data) => {
  state.items = Array.isArray(data?.items) ? data.items : []
  state.summary = {
    pending: Number(data?.summary?.pending || 0),
    authorized: Number(data?.summary?.authorized || 0),
    consumed: Number(data?.summary?.consumed || 0),
    reauthorize_required: Number(data?.summary?.reauthorize_required || 0),
  }
  state.page = Number(data?.page || 1)
  state.pageSize = Number(data?.page_size || PAGE_SIZE)
  state.total = Number(data?.total || 0)
  state.loaded = true
}

const updateMetadata = (data) => {
  metadata.account = String(data?.account || '')
  metadata.addedDate = String(data?.added_date || '')
  metadata.planEndDate = String(data?.plan_end_date || '')
  metadata.startTime = String(data?.schedule?.start_time || '')
  metadata.endTime = String(data?.schedule?.end_time || '')
}

const loadFuture = async ({ refreshPlan = false } = {}) => {
  future.loading = true
  try {
    const response = await apiClient.value.get(apiBase.value, {
      params: {
        scope: 'future',
        page: future.page,
        page_size: future.pageSize,
        status: future.status || undefined,
        target_type: future.targetType || undefined,
        refresh_plan: refreshPlan,
      },
    })
    updateState(future, response.data)
    updateMetadata(response.data)
    return true
  } catch (error) {
    notifyError(resolveErrorMessage(error, '加载预授权列表失败'))
    return false
  } finally {
    future.loading = false
  }
}

const loadPast = async () => {
  past.loading = true
  try {
    const response = await apiClient.value.get(apiBase.value, {
      params: {
        scope: 'past',
        page: past.page,
        page_size: past.pageSize,
        status: past.status || undefined,
      },
    })
    updateState(past, response.data)
    updateMetadata(response.data)
    return true
  } catch (error) {
    notifyError(resolveErrorMessage(error, '加载历史补卡预授权失败'))
    return false
  } finally {
    past.loading = false
  }
}

const refreshPlan = async () => {
  future.page = 1
  const futureLoaded = await loadFuture({ refreshPlan: true })
  if (!futureLoaded) return
  if (pastExpanded.value) {
    past.page = 1
    const pastLoaded = await loadPast()
    if (!pastLoaded) return
  } else {
    past.loaded = false
  }
  notifySuccess('实习计划已同步')
}

const togglePast = async () => {
  pastExpanded.value = !pastExpanded.value
  if (pastExpanded.value && !past.loaded) await loadPast()
}

const pageFuture = async (page) => {
  future.page = page
  await loadFuture()
}

const pagePast = async (page) => {
  past.page = page
  await loadPast()
}

const applyFutureFilters = async () => {
  future.page = 1
  await loadFuture()
}

const resetFutureFilters = async () => {
  future.status = ''
  future.targetType = ''
  await applyFutureFilters()
}

const applyPastFilters = async () => {
  past.page = 1
  await loadPast()
}

const resetPastFilters = async () => {
  past.status = ''
  await applyPastFilters()
}

const targetKey = (item) => `${item?.target_date || ''}-${item?.target_type || ''}`

const startAuthorization = async (item) => {
  if (!item?.can_authorize || startingKey.value) return
  startingKey.value = targetKey(item)
  try {
    const response = await apiClient.value.post(`${apiBase.value}/start`, {
      target_date: item.target_date,
      target_type: item.target_type,
    })
    authorizationSession.value = {
      registrationTicket: String(response.data?.registration_ticket || ''),
      directUrl: String(response.data?.direct_url || ''),
      browserUrl: String(response.data?.browser_url || ''),
      startedAt: String(response.data?.started_at || ''),
      expiresAt: String(response.data?.expires_at || ''),
    }
    selectedTarget.value = {
      date: String(item.target_date || ''),
      type: String(item.target_type || ''),
      scope: item.target_type === 'MAKEUP' ? 'past' : 'future',
    }
    dialogVisible.value = true
  } catch (error) {
    notifyError(resolveErrorMessage(error, '发起支付宝预授权失败'))
  } finally {
    startingKey.value = ''
  }
}

const clearAuthorizationSession = () => {
  authorizationSession.value = null
  selectedTarget.value = null
}

const completeAuthorization = async () => {
  const ticket = authorizationSession.value?.registrationTicket
  if (!ticket || completing.value) return
  completing.value = true
  try {
    await apiClient.value.post(`${apiBase.value}/complete`, {
      registration_ticket: ticket,
    })
    const scope = selectedTarget.value?.scope
    dialogVisible.value = false
    notifySuccess('预授权已登记')
    if (scope === 'past') await loadPast()
    else await loadFuture()
  } catch (error) {
    notifyError(resolveErrorMessage(error, '确认预授权失败'))
  } finally {
    completing.value = false
  }
}

onMounted(loadFuture)
</script>

<template>
  <section class="preauthorization-page">
    <header class="page-header">
      <div>
        <h1 class="page-title">打卡预授权</h1>
        <p class="page-subtitle">
          {{ metadata.account || '当前账号' }} · {{ metadata.addedDate || '-' }} 至 {{ metadata.planEndDate || '-' }}
        </p>
      </div>
      <el-button :loading="future.loading" @click="refreshPlan">
        同步实习计划
      </el-button>
    </header>

    <div class="summary-strip" aria-label="未来预授权状态汇总">
      <div v-for="item in futureSummary" :key="item.key" class="summary-item">
        <span class="summary-label">{{ item.label }}</span>
        <strong class="summary-value">{{ item.value }}</strong>
      </div>
    </div>

    <section class="list-section">
      <div class="section-heading">
        <div>
          <h2 class="section-title">今天及未来</h2>
          <p class="section-description">
            上班 {{ metadata.startTime || '-' }}，下班 {{ metadata.endTime || '-' }}
          </p>
        </div>
        <span class="section-count">共 {{ future.total }} 项</span>
      </div>
      <div class="filter-row" aria-label="未来预授权筛选">
        <el-select v-model="future.status" clearable placeholder="全部状态" size="small">
          <el-option label="待授权" value="pending" />
          <el-option label="已授权" value="authorized" />
          <el-option label="已使用" value="consumed" />
          <el-option label="需重新授权" value="reauthorize_required" />
        </el-select>
        <el-select v-model="future.targetType" clearable placeholder="全部类型" size="small">
          <el-option label="上班" value="START" />
          <el-option label="下班" value="END" />
        </el-select>
        <el-button size="small" type="primary" @click="applyFutureFilters">筛选</el-button>
        <el-button size="small" @click="resetFutureFilters">清除</el-button>
      </div>
      <ClockInPreauthorizationList
        :items="future.items"
        :loading="future.loading || !!startingKey"
        :page="future.page"
        :page-size="future.pageSize"
        :total="future.total"
        empty-text="当前没有需要预授权的未来日期"
        @authorize="startAuthorization"
        @page-change="pageFuture"
      />
    </section>

    <section class="past-section">
      <button
        type="button"
        class="past-toggle"
        :aria-expanded="pastExpanded"
        @click="togglePast"
      >
        <span>
          <strong>过去日期与补卡</strong>
          <small>每天只需授权一次，可用于当日上班或下班补卡</small>
        </span>
        <span class="toggle-state">{{ pastExpanded ? '收起' : '展开' }}</span>
      </button>
      <el-collapse-transition>
        <div v-show="pastExpanded" class="past-content">
          <div class="filter-row" aria-label="历史预授权筛选">
            <el-select v-model="past.status" clearable placeholder="全部状态" size="small">
              <el-option label="待授权" value="pending" />
              <el-option label="已授权" value="authorized" />
              <el-option label="已使用" value="consumed" />
              <el-option label="需重新授权" value="reauthorize_required" />
            </el-select>
            <el-button size="small" type="primary" @click="applyPastFilters">筛选</el-button>
            <el-button size="small" @click="resetPastFilters">清除</el-button>
          </div>
          <ClockInPreauthorizationList
            :items="past.items"
            :loading="past.loading || !!startingKey"
            :page="past.page"
            :page-size="past.pageSize"
            :total="past.total"
            empty-text="没有可用于补卡的过去日期"
            @authorize="startAuthorization"
            @page-change="pagePast"
          />
        </div>
      </el-collapse-transition>
    </section>

    <ClockInPreauthorizationDialog
      v-model="dialogVisible"
      :direct-url="authorizationSession?.directUrl || ''"
      :browser-url="authorizationSession?.browserUrl || ''"
      :started-at="authorizationSession?.startedAt || ''"
      :expires-at="authorizationSession?.expiresAt || ''"
      :target-date="selectedTarget?.date || ''"
      :target-type="selectedTarget?.type || ''"
      :completing="completing"
      @complete="completeAuthorization"
      @closed="clearAuthorizationSession"
    />
  </section>
</template>

<style scoped>
.preauthorization-page {
  display: grid;
  gap: 18px;
  min-width: 0;
}

.page-header,
.section-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
}

.page-title,
.section-title {
  margin: 0;
  color: var(--el-text-color-primary);
  letter-spacing: 0;
}

.page-title {
  font-size: 22px;
  line-height: 1.3;
}

.section-title {
  font-size: 16px;
  line-height: 1.4;
}

.page-subtitle,
.section-description {
  margin: 5px 0 0;
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-text-color-secondary);
}

.summary-strip {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  border-top: 1px solid var(--el-border-color-light);
  border-bottom: 1px solid var(--el-border-color-light);
}

.summary-item {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  min-width: 0;
  padding: 12px 16px;
  border-right: 1px solid var(--el-border-color-light);
}

.summary-item:last-child {
  border-right: 0;
}

.summary-label,
.section-count {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.summary-value {
  font-size: 20px;
  color: var(--el-text-color-primary);
}

.list-section,
.past-section {
  padding: 16px;
  border: 1px solid var(--el-border-color-light);
  border-radius: 6px;
  background: var(--el-bg-color);
}

.section-heading {
  margin-bottom: 14px;
}

.filter-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}

.filter-row :deep(.el-select) {
  width: 150px;
}

.past-section {
  padding: 0;
  overflow: hidden;
}

.past-toggle {
  width: 100%;
  min-height: 68px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 14px 16px;
  border: 0;
  background: transparent;
  color: var(--el-text-color-primary);
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.past-toggle:hover {
  background: var(--el-fill-color-light);
}

.past-toggle span:first-child {
  display: grid;
  gap: 4px;
}

.past-toggle small,
.toggle-state {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.toggle-state {
  flex: 0 0 auto;
}

.past-content {
  padding: 0 16px 16px;
  border-top: 1px solid var(--el-border-color-lighter);
}

.past-content > .filter-row {
  padding-top: 16px;
}

@media (max-width: 768px) {
  .page-header,
  .section-heading {
    align-items: flex-start;
  }

  .page-header :deep(.el-button) {
    flex: 0 0 auto;
  }

  .summary-strip {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .summary-item:nth-child(2) {
    border-right: 0;
  }

  .summary-item:nth-child(-n + 2) {
    border-bottom: 1px solid var(--el-border-color-light);
  }

  .list-section {
    padding: 14px;
  }

  .past-content {
    padding: 0 14px 14px;
  }

  .filter-row :deep(.el-select) {
    flex: 1 1 130px;
    width: auto;
  }
}

@media (max-width: 420px) {
  .page-header {
    display: grid;
  }

  .page-header :deep(.el-button) {
    width: 100%;
  }
}
</style>
