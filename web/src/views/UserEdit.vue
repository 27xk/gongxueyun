<template>
  <el-card class="page-card">
    <template #header>
      <div class="page-header">
        <div class="page-title">{{ isEdit ? '编辑用户' : '添加用户' }}</div>
        <div class="page-actions">
          <el-button @click="goBack">返回</el-button>
        </div>
      </div>
    </template>

    <el-form
      :model="form"
      :label-width="isMobile ? 'auto' : '120px'"
      :label-position="isMobile ? 'top' : 'right'"
      v-loading="loading"
    >
      <el-tabs v-model="activeTab">
        <el-tab-pane label="基本信息" name="basic">
          <el-form-item label="账号">
            <el-input v-model="form.phone" placeholder="请输入工学云账号" />
          </el-form-item>
          <el-form-item label="密码">
            <el-input v-model="form.password" :placeholder="passwordPlaceholder" />
          </el-form-item>
          <el-form-item label="备注">
            <el-input v-model="form.remark" type="textarea" :rows="2" placeholder="例如：张三/研发部/某项目（支持搜索）" />
          </el-form-item>
          <el-form-item label="启用打卡">
            <el-switch v-model="form.enable_clockin" active-text="开启自动任务" />
          </el-form-item>
        </el-tab-pane>

        <el-tab-pane label="打卡设置" name="clockin">
          <el-alert title="在地图上搜索或点击可自动获取经纬度和地址" type="info" :closable="false" style="margin-bottom: 15px;" />
          
          <el-form-item label="详细地址">
            <div class="place-row">
              <el-input
                class="place-input"
                v-model="searchQuery"
                placeholder="例如：湖南省 · 长沙市 · 岳麓区 · 在麓谷企业广场附近"
                @keyup.enter="searchPlace"
              />
              <div class="place-actions">
                <el-button type="primary" @click="searchPlace">搜索</el-button>
                <el-button :disabled="!isEdit" :loading="addrFillLoading" @click="fillFromAccountAddress">账号地址填充</el-button>
              </div>
            </div>
          </el-form-item>

          <div id="map" class="map"></div>

          <el-form-item label="上班打卡">
            <el-time-select v-model="form.clockIn.schedule.startTime" start="00:00" step="00:01" end="23:59" />
          </el-form-item>
          <el-form-item label="下班打卡">
            <el-time-select v-model="form.clockIn.schedule.endTime" start="00:00" step="00:01" end="23:59" />
          </el-form-item>
          <el-form-item v-if="isEdit" label="补卡日期">
            <div class="clockin-backfill">
              <div class="clockin-backfill-row">
                <el-select v-model="clockInMakeupType" class="clockin-type-select" placeholder="补卡类型" @change="syncClockInTargetFromOptions">
                  <el-option label="上班" value="START" />
                  <el-option label="下班" value="END" />
                </el-select>
                <el-select v-model="clockInTargetDates" :loading="clockInPeriodLoading" multiple collapse-tags collapse-tags-tooltip filterable placeholder="选择待补日期">
                  <el-option v-for="item in filteredClockInPeriodOptions" :key="item.value" :label="item.label" :value="item.value" />
                </el-select>
                <el-button :loading="clockInPeriodLoading" @click="loadClockInMissingDays">刷新缺卡</el-button>
                <el-button type="warning" :loading="clockInMakeupLoading" :disabled="!clockInTargetDates.length" @click="makeupClockIn">补选中</el-button>
                <el-button type="danger" :loading="clockInMakeupAllLoading" :disabled="!filteredClockInPeriodOptions.length" @click="makeupAllClockIn">全部待补</el-button>
              </div>
              <el-progress
                v-if="clockInMakeupAllLoading || clockInMakeupAllPercent > 0"
                class="clockin-backfill-progress"
                :percentage="clockInMakeupAllPercent"
                :status="clockInMakeupAllPercent >= 100 ? 'success' : undefined"
              />
              <div class="clockin-backfill-hint">
                已获取 {{ clockInRecordCount }} 条打卡记录，{{ clockInMakeupTypeLabel }}待补 {{ filteredClockInPeriodOptions.length }} 天，已选 {{ clockInTargetDates.length }} 天
              </div>
            </div>
          </el-form-item>
          <el-form-item label="打卡周期">
            <el-checkbox-group v-model="form.clockIn.schedule.weekdays" class="week-group">
              <el-checkbox v-for="d in weekdayOptions" :key="d.value" :value="d.value">{{ d.label }}</el-checkbox>
            </el-checkbox-group>
          </el-form-item>
          <el-form-item label="打卡天数">
            <el-input-number v-model="form.clockIn.schedule.totalDays" :min="1" :max="3650" />
          </el-form-item>
          <el-row>
            <el-col :xs="24" :sm="12">
              <el-form-item label="纬度">
                <el-input v-model="form.clockIn.location.latitude" />
              </el-form-item>
            </el-col>
            <el-col :xs="24" :sm="12">
              <el-form-item label="经度">
                <el-input v-model="form.clockIn.location.longitude" />
              </el-form-item>
            </el-col>
          </el-row>
          <el-row>
             <el-col :xs="24" :sm="8">
               <el-form-item label="省份">
                <el-input v-model="form.clockIn.location.province" />
              </el-form-item>
             </el-col>
             <el-col :xs="24" :sm="8">
               <el-form-item label="城市">
                <el-input v-model="form.clockIn.location.city" />
              </el-form-item>
             </el-col>
             <el-col :xs="24" :sm="8">
               <el-form-item label="区域">
                <el-input v-model="form.clockIn.location.area" />
              </el-form-item>
             </el-col>
          </el-row>
        </el-tab-pane>

        <el-tab-pane label="报告设置" name="report">
          <el-divider content-position="left">日报</el-divider>
          <el-form-item label="启用日报">
            <el-switch v-model="form.reportSettings.daily.enabled" />
          </el-form-item>
          <el-form-item v-if="form.reportSettings.daily.enabled" label="提交时间">
            <el-time-select v-model="form.reportSettings.daily.submitTime" start="00:00" step="00:01" end="23:59" />
          </el-form-item>
          <el-form-item v-if="form.reportSettings.daily.enabled" label="提交日(周)">
            <el-checkbox-group v-model="form.reportSettings.daily.submitDays" class="week-group">
              <el-checkbox v-for="d in weekdayOptions" :key="d.value" :value="d.value">{{ d.label }}</el-checkbox>
            </el-checkbox-group>
          </el-form-item>
          <el-form-item label="补交日期">
            <div class="report-backfill">
              <div class="report-backfill-row">
                <el-select v-model="reportTargets.daily" :loading="reportPeriodLoading.daily" filterable placeholder="选择未提交日报日期">
                  <el-option v-for="item in reportPeriodOptions.daily" :key="item.value" :label="item.label" :value="item.value" />
                </el-select>
                <el-button :loading="reportPeriodLoading.daily" :disabled="!isEdit || !!reportMakeupAllLoading" @click="loadReportPeriodOptions('daily')">刷新待补</el-button>
                <el-button type="danger" :loading="reportMakeupAllLoading === 'daily'" :disabled="!isEdit || !!reportRunLoading || !!reportActionLoading || (!!reportMakeupAllLoading && reportMakeupAllLoading !== 'daily') || !reportPendingCount('daily')" @click="makeupAllReports('daily')">补全部日报</el-button>
              </div>
              <el-progress
                v-if="reportMakeupAllInProgress('daily')"
                class="report-backfill-progress"
                :percentage="reportMakeupAllPercent"
                :status="reportMakeupAllPercent >= 100 ? 'success' : undefined"
              />
              <div class="report-backfill-hint">日报待补 {{ reportPendingCount('daily') }} 个周期</div>
            </div>
          </el-form-item>
          <el-form-item label="日报预览">
            <div class="report-preview">
              <div class="report-preview-meta">
                <div>字数：{{ dailyCount }} / 1000</div>
                <div class="report-preview-actions">
                  <el-button size="small" :disabled="!isEdit || !!reportRunLoading || !!reportMakeupAllLoading || !reportTargets.daily" :loading="reportActionLoading === 'daily_generate'" @click="generateReport('daily')">
                    AI生成日报
                  </el-button>
                  <el-button
                    size="small"
                    type="primary"
                    :disabled="!isEdit || !!reportRunLoading || !!reportMakeupAllLoading || !reportTargets.daily || !String(reportPreview.daily || '').trim()"
                    :loading="reportActionLoading === 'daily_submit'"
                    @click="submitReport('daily')"
                  >
                    提交日报
                  </el-button>
                  <el-button
                    size="small"
                    type="success"
                    :disabled="!isEdit || !!reportRunLoading || !!reportMakeupAllLoading || !reportTargets.daily || aiDailyLoading || submitDailyLoading"
                    :loading="reportRunLoading === 'daily_report'"
                    @click="runReportNow('daily_report')"
                  >
                    立即生成并提交
                  </el-button>
                </div>
              </div>
              <el-input
                v-model="reportPreview.daily"
                type="textarea"
                :rows="6"
                resize="none"
                placeholder="点击 AI生成日报 或手动填写后提交"
              />
            </div>
          </el-form-item>
          
          <el-divider content-position="left">周报</el-divider>
          <el-form-item label="启用周报">
            <el-switch v-model="form.reportSettings.weekly.enabled" />
          </el-form-item>
          <el-form-item label="提交时间(周几)">
             <el-input-number v-model="form.reportSettings.weekly.submitTime" :min="1" :max="7" />
          </el-form-item>
          <el-form-item v-if="form.reportSettings.weekly.enabled" label="提交时刻">
            <el-time-select v-model="form.reportSettings.weekly.submitAt" start="00:00" step="00:01" end="23:59" />
          </el-form-item>
          <el-form-item label="补交周">
            <div class="report-backfill">
              <div class="report-backfill-row">
                <el-select v-model="reportTargets.weekly" :loading="reportPeriodLoading.weekly" filterable placeholder="选择未提交周报周期">
                  <el-option v-for="item in reportPeriodOptions.weekly" :key="item.value" :label="item.label" :value="item.value" />
                </el-select>
                <el-button :loading="reportPeriodLoading.weekly" :disabled="!isEdit || !!reportMakeupAllLoading" @click="loadReportPeriodOptions('weekly')">刷新待补</el-button>
                <el-button type="danger" :loading="reportMakeupAllLoading === 'weekly'" :disabled="!isEdit || !!reportRunLoading || !!reportActionLoading || (!!reportMakeupAllLoading && reportMakeupAllLoading !== 'weekly') || !reportPendingCount('weekly')" @click="makeupAllReports('weekly')">补全部周报</el-button>
              </div>
              <el-progress
                v-if="reportMakeupAllInProgress('weekly')"
                class="report-backfill-progress"
                :percentage="reportMakeupAllPercent"
                :status="reportMakeupAllPercent >= 100 ? 'success' : undefined"
              />
              <div class="report-backfill-hint">周报待补 {{ reportPendingCount('weekly') }} 个周期</div>
            </div>
          </el-form-item>
          <el-form-item label="周报预览">
            <div class="report-preview">
              <div class="report-preview-meta">
                <div>字数：{{ weeklyCount }} / 1000</div>
                <div class="report-preview-actions">
                  <el-button size="small" :disabled="!isEdit || !!reportRunLoading || !!reportMakeupAllLoading || !reportTargets.weekly" :loading="reportActionLoading === 'weekly_generate'" @click="generateReport('weekly')">
                    AI生成周报
                  </el-button>
                  <el-button
                    size="small"
                    type="primary"
                    :disabled="!isEdit || !!reportRunLoading || !!reportMakeupAllLoading || !reportTargets.weekly || !String(reportPreview.weekly || '').trim()"
                    :loading="reportActionLoading === 'weekly_submit'"
                    @click="submitReport('weekly')"
                  >
                    提交周报
                  </el-button>
                  <el-button
                    size="small"
                    type="success"
                    :disabled="!isEdit || !!reportRunLoading || !!reportMakeupAllLoading || !reportTargets.weekly"
                    :loading="reportRunLoading === 'weekly_report'"
                    @click="runReportNow('weekly_report')"
                  >
                    立即执行周报
                  </el-button>
                </div>
              </div>
              <el-input v-model="reportPreview.weekly" type="textarea" :rows="6" resize="none" placeholder="点击 AI生成周报 或手动填写后提交" />
            </div>
          </el-form-item>

           <el-divider content-position="left">月报</el-divider>
          <el-form-item label="启用月报">
            <el-switch v-model="form.reportSettings.monthly.enabled" />
          </el-form-item>
          <el-form-item label="提交时间(号)">
             <el-input-number v-model="form.reportSettings.monthly.submitTime" :min="1" :max="31" />
          </el-form-item>
          <el-form-item v-if="form.reportSettings.monthly.enabled" label="提交时刻">
            <el-time-select v-model="form.reportSettings.monthly.submitAt" start="00:00" step="00:01" end="23:59" />
          </el-form-item>
          <el-form-item label="补交月份">
            <div class="report-backfill">
              <div class="report-backfill-row">
                <el-select v-model="reportTargets.monthly" :loading="reportPeriodLoading.monthly" filterable placeholder="选择未提交月报月份">
                  <el-option v-for="item in reportPeriodOptions.monthly" :key="item.value" :label="item.label" :value="item.value" />
                </el-select>
                <el-button :loading="reportPeriodLoading.monthly" :disabled="!isEdit || !!reportMakeupAllLoading" @click="loadReportPeriodOptions('monthly')">刷新待补</el-button>
                <el-button type="danger" :loading="reportMakeupAllLoading === 'monthly'" :disabled="!isEdit || !!reportRunLoading || !!reportActionLoading || (!!reportMakeupAllLoading && reportMakeupAllLoading !== 'monthly') || !reportPendingCount('monthly')" @click="makeupAllReports('monthly')">补全部月报</el-button>
              </div>
              <el-progress
                v-if="reportMakeupAllInProgress('monthly')"
                class="report-backfill-progress"
                :percentage="reportMakeupAllPercent"
                :status="reportMakeupAllPercent >= 100 ? 'success' : undefined"
              />
              <div class="report-backfill-hint">月报待补 {{ reportPendingCount('monthly') }} 个周期</div>
            </div>
          </el-form-item>
          <el-form-item label="月报预览">
            <div class="report-preview">
              <div class="report-preview-meta">
                <div>字数：{{ monthlyCount }} / 1000</div>
                <div class="report-preview-actions">
                  <el-button size="small" :disabled="!isEdit || !!reportRunLoading || !!reportMakeupAllLoading || !reportTargets.monthly" :loading="reportActionLoading === 'monthly_generate'" @click="generateReport('monthly')">
                    AI生成月报
                  </el-button>
                  <el-button
                    size="small"
                    type="primary"
                    :disabled="!isEdit || !!reportRunLoading || !!reportMakeupAllLoading || !reportTargets.monthly || !String(reportPreview.monthly || '').trim()"
                    :loading="reportActionLoading === 'monthly_submit'"
                    @click="submitReport('monthly')"
                  >
                    提交月报
                  </el-button>
                  <el-button
                    size="small"
                    type="success"
                    :disabled="!isEdit || !!reportRunLoading || !!reportMakeupAllLoading || !reportTargets.monthly"
                    :loading="reportRunLoading === 'monthly_report'"
                    @click="runReportNow('monthly_report')"
                  >
                    立即执行月报
                  </el-button>
                </div>
              </div>
              <el-input v-model="reportPreview.monthly" type="textarea" :rows="6" resize="none" placeholder="点击 AI生成月报 或手动填写后提交" />
            </div>
          </el-form-item>
        </el-tab-pane>
        
        <el-tab-pane label="AI 设置" name="ai">
             <el-form-item label="Model">
                <el-input v-model="form.ai.model" placeholder="gpt-4o-mini" />
             </el-form-item>
             <el-form-item label="API Key">
                <el-input v-model="form.ai.apikey" :placeholder="secretPlaceholder" />
             </el-form-item>
              <el-form-item label="API URL">
                <el-input v-model="form.ai.apiUrl" placeholder="https://api.openai.com/ 或 https://api-inference.modelscope.cn/v1" />
             </el-form-item>
             <el-form-item label="测试">
                <div class="ai-test-row">
                  <el-button type="primary" :loading="aiTestLoading" @click="testAi">测试 AI</el-button>
                  <el-button @click="applyModelScopePreset">魔搭预设</el-button>
                  <el-tag v-if="aiTestStatus" size="small" :type="aiTestStatus === 'ok' ? 'success' : 'danger'">
                    {{ aiTestStatus === 'ok' ? '可用' : '不可用' }}
                  </el-tag>
                  <span v-if="aiTestLatencyMs !== null" class="ai-test-meta">延迟：{{ aiTestLatencyMs }}ms</span>
                </div>
             </el-form-item>
        </el-tab-pane>

        <el-tab-pane label="推送设置" name="push">
          <el-alert title="仅保留 Server酱 和 QQ 邮箱 SMTP 推送；QQ 邮箱 SMTP 的发件账号由管理员统一配置，这里只设置开关和收件邮箱。" type="info" :closable="false" style="margin-bottom: 15px;" />

          <el-divider content-position="left">Server酱</el-divider>
          <el-form-item label="启用 Server酱">
            <el-switch v-model="form.pushNotifications[0].enabled" />
          </el-form-item>
          <el-form-item label="SendKey">
            <el-input v-model="form.pushNotifications[0].sendKey" :placeholder="pushSecretPlaceholder" clearable />
          </el-form-item>

          <el-divider content-position="left">QQ 邮箱 SMTP</el-divider>
          <el-form-item label="启用 SMTP">
            <el-switch v-model="form.pushNotifications[1].enabled" />
          </el-form-item>
          <el-form-item label="收件邮箱">
            <el-input v-model="form.pushNotifications[1].to" placeholder="例如：demo@qq.com" />
          </el-form-item>
        </el-tab-pane>
      </el-tabs>

      <el-form-item v-if="!isMobile" class="desktop-actions">
        <div class="form-actions">
          <el-button type="primary" @click="save">保存</el-button>
          <el-button @click="cancelEdit">取消</el-button>
        </div>
      </el-form-item>

      <div v-if="isMobile" class="mobile-actions-spacer"></div>
    </el-form>
  </el-card>

  <div v-if="isMobile" class="mobile-bottom-actions">
    <div class="mobile-bottom-actions-inner">
      <el-button type="primary" style="flex: 1" @click="save">保存</el-button>
      <el-button style="flex: 1" @click="cancelEdit">取消</el-button>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, onUnmounted, computed, nextTick, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { http } from '../api/http'
import { parseCnDotAddress, formatCnDotAddress } from '../utils/cnAddress'
import { notifySuccess, notifyError, notifyWarning, notifyInfo, resolveErrorMessage } from '../utils/notify'

const route = useRoute()
const router = useRouter()
const isEdit = computed(() => !!route.params.id)
const passwordPlaceholder = computed(() => '请输入工学云密码')
const secretPlaceholder = computed(() => '请输入')
const pushSecretPlaceholder = computed(() => '请输入')
const loading = ref(false)
const activeTab = ref('basic')
const mapInstance = ref(null)
const marker = ref(null)
let L = null
const isMobile = ref(false)
const aiTestLoading = ref(false)
const aiTestStatus = ref('')
const aiTestLatencyMs = ref(null)
const addrFillLoading = ref(false)
const aiDailyLoading = ref(false)
const submitDailyLoading = ref(false)
const clockInPeriodLoading = ref(false)
const clockInMakeupLoading = ref(false)
const clockInMakeupAllLoading = ref(false)
const clockInMakeupAllPercent = ref(0)
const clockInMakeupType = ref('START')
const clockInPeriodOptions = ref([])
const clockInTargetDates = ref([])
const clockInRecordCount = ref(0)
const reportRunLoading = ref('')
const reportActionLoading = ref('')
const reportPreview = reactive({ daily: '', weekly: '', monthly: '' })
const reportPeriodLoading = reactive({ daily: false, weekly: false, monthly: false })
const reportPeriodOptions = reactive({ daily: [], weekly: [], monthly: [] })
const reportMakeupAllLoading = ref('')
const reportMakeupAllPercent = ref(0)
const reportMakeupAllProgressKey = ref('')
const CLOCKIN_MAKEUP_REQUEST_TIMEOUT = 0
const REPORT_MAKEUP_REQUEST_TIMEOUT = 0
const _today = new Date()
const _pad2 = (n) => String(n).padStart(2, '0')
const _todayDate = `${_today.getFullYear()}-${_pad2(_today.getMonth() + 1)}-${_pad2(_today.getDate())}`
const _todayMonth = `${_today.getFullYear()}-${_pad2(_today.getMonth() + 1)}`
const reportTargets = reactive({ daily: _todayDate, weekly: _todayDate, monthly: _todayMonth })
let geocodeSearchAbort = null
let geocodeReverseAbort = null
let clockInMakeupAllTimer = null
let reportMakeupAllTimer = null

const resetClockInMakeupAllProgress = () => {
  if (clockInMakeupAllTimer) {
    clearInterval(clockInMakeupAllTimer)
    clockInMakeupAllTimer = null
  }
  clockInMakeupAllPercent.value = 0
}

const startClockInMakeupAllProgress = (total) => {
  resetClockInMakeupAllProgress()
  const count = Math.max(Number(total || 0), 1)
  const estimatedMs = Math.max(count * 2200, 3000)
  const startedAt = Date.now()
  clockInMakeupAllPercent.value = 3
  clockInMakeupAllTimer = setInterval(() => {
    const elapsed = Date.now() - startedAt
    clockInMakeupAllPercent.value = Math.min(95, Math.max(3, Math.round((elapsed / estimatedMs) * 95)))
  }, 300)
}

const finishClockInMakeupAllProgress = () => {
  if (clockInMakeupAllTimer) {
    clearInterval(clockInMakeupAllTimer)
    clockInMakeupAllTimer = null
  }
  clockInMakeupAllPercent.value = 100
  window.setTimeout(() => {
    if (!clockInMakeupAllLoading.value) clockInMakeupAllPercent.value = 0
  }, 1200)
}

const resetReportMakeupAllProgress = () => {
  if (reportMakeupAllTimer) {
    clearInterval(reportMakeupAllTimer)
    reportMakeupAllTimer = null
  }
  reportMakeupAllPercent.value = 0
  reportMakeupAllProgressKey.value = ''
}

const startReportMakeupAllProgress = (key, total) => {
  resetReportMakeupAllProgress()
  const count = Math.max(Number(total || 0), 1)
  const estimatedMs = Math.max(count * 4500, 5000)
  const startedAt = Date.now()
  reportMakeupAllProgressKey.value = key
  reportMakeupAllPercent.value = 3
  reportMakeupAllTimer = setInterval(() => {
    const elapsed = Date.now() - startedAt
    reportMakeupAllPercent.value = Math.min(95, Math.max(3, Math.round((elapsed / estimatedMs) * 95)))
  }, 300)
}

const finishReportMakeupAllProgress = () => {
  if (reportMakeupAllTimer) {
    clearInterval(reportMakeupAllTimer)
    reportMakeupAllTimer = null
  }
  reportMakeupAllPercent.value = 100
  window.setTimeout(() => {
    if (!reportMakeupAllLoading.value) resetReportMakeupAllProgress()
  }, 1200)
}

const flushUiMessage = async () => {
  await nextTick()
  await new Promise((resolve) => requestAnimationFrame(() => resolve()))
}

const _countText = (t) => {
  const s = String(t || '').replace(/\s+/g, '')
  return Math.min(1000, s.length)
}
const dailyCount = computed(() => _countText(reportPreview.daily))
const weeklyCount = computed(() => _countText(reportPreview.weekly))
const monthlyCount = computed(() => _countText(reportPreview.monthly))
const weekdayOptions = [
  { label: '周一', value: 1 },
  { label: '周二', value: 2 },
  { label: '周三', value: 3 },
  { label: '周四', value: 4 },
  { label: '周五', value: 5 },
  { label: '周六', value: 6 },
  { label: '周日', value: 7 },
]

const buildDefaultPushNotifications = () => ([
  { type: 'Server', enabled: false, sendKey: '' },
  { type: 'SMTP', enabled: false, to: '' },
])

const normalizePushNotifications = (items) => {
  const list = Array.isArray(items) ? items : []
  const map = new Map(
    list
      .filter((item) => item && typeof item === 'object' && item.type)
      .map((item) => [item.type, { ...item }])
  )
  const server = map.get('Server') || {}
  const smtp = map.get('SMTP') || {}
  return [
    {
      type: 'Server',
      enabled: !!server.enabled,
      sendKey: typeof server.sendKey === 'string' ? server.sendKey : '',
    },
    {
      type: 'SMTP',
      enabled: !!smtp.enabled,
      to: typeof smtp.to === 'string' ? smtp.to : '',
    },
  ]
}

const form = reactive({
  phone: '',
  password: '',
  remark: '',
  enable_clockin: true,
  clockIn: {
    mode: 'custom',
    location: {
      address: '', latitude: '', longitude: '', province: '', city: '', area: ''
    },
    imageCount: 0,
    description: [],
    specialClockIn: false,
    customDays: [],
    schedule: {
      startTime: '08:30',
      endTime: '18:30',
      weekdays: [1, 2, 3, 4, 5, 6, 7],
      totalDays: 180,
      startDate: ''
    }
  },
  reportSettings: {
    daily: { enabled: false, imageCount: 0, submitTime: '12:00', submitDays: [1, 2, 3, 4, 5, 6, 7] },
    weekly: { enabled: true, imageCount: 0, submitTime: 5, submitAt: '18:30' },
    monthly: { enabled: false, imageCount: 0, submitTime: 29, submitAt: '12:00' }
  },
  ai: {
      model: "gpt-4o-mini",
      apikey: "",
      apiUrl: "https://api.openai.com/"
  },
  pushNotifications: buildDefaultPushNotifications(),
  device: "{brand: TA J20, systemVersion: 17, Platform: Android, isPhysicalDevice: true, incremental: K23V10A}"
})

const searchQuery = computed({
  get: () => form.clockIn?.location?.address || '',
  set: (v) => {
    form.clockIn.location.address = String(v ?? '')
  },
})

const _cleanSegment = (v) => String(v || '').replace(/\s+/g, ' ').trim()

const _dedupeSegments = (segments) => {
  const out = []
  for (const s of segments) {
    const seg = _cleanSegment(s)
    if (!seg) continue
    if (out.length && out[out.length - 1] === seg) continue
    out.push(seg)
  }
  return out
}

const _composeAddress = (segments) => _dedupeSegments(segments).join(' · ')

const _pickFirst = (...vals) => {
  for (const v of vals) {
    const s = _cleanSegment(v)
    if (s) return s
  }
  return ''
}

const applyAddressStruct = (rawAddress, opts = {}) => {
  const input = String(rawAddress ?? '')
  const hasDot = /[·•]/.test(input)
  if (!hasDot && !opts?.force) return
  const parsed = parseCnDotAddress(input)
  if (parsed?.province) form.clockIn.location.province = parsed.province
  if (parsed?.province === '北京' || parsed?.province === '天津' || parsed?.province === '上海' || parsed?.province === '重庆') {
    form.clockIn.location.city = ''
  } else if (parsed?.city) {
    form.clockIn.location.city = parsed.city
  }
  if (parsed?.district) form.clockIn.location.area = parsed.district

  const rewriteAddress = typeof opts?.rewriteAddress === 'boolean' ? opts.rewriteAddress : hasDot
  if (rewriteAddress) {
    const normalized = formatCnDotAddress(parsed)
    if (normalized) form.clockIn.location.address = normalized
  }
}

const ensureLeaflet = async () => {
  if (L) return L
  const mod = await import('leaflet')
  await import('leaflet/dist/leaflet.css')
  const icon = (await import('leaflet/dist/images/marker-icon.png')).default
  const iconShadow = (await import('leaflet/dist/images/marker-shadow.png')).default
  L = mod.default
  const DefaultIcon = L.icon({
    iconUrl: icon,
    shadowUrl: iconShadow,
    iconSize: [25, 41],
    iconAnchor: [12, 41],
  })
  L.Marker.prototype.options.icon = DefaultIcon
  return L
}

const updateLocation = async (lat, lng, label = '') => {
    const Leaflet = await ensureLeaflet()
    lat = parseFloat(lat);
    lng = parseFloat(lng);

    if (marker.value) {
        marker.value.setLatLng([lat, lng]);
    } else {
        marker.value = Leaflet.marker([lat, lng]).addTo(mapInstance.value);
    }
    
    form.clockIn.location.latitude = lat.toFixed(6);
    form.clockIn.location.longitude = lng.toFixed(6);
    
    if (label) {
        form.clockIn.location.address = _composeAddress(String(label).split(/[·,，]/g));
        applyAddressStruct(form.clockIn.location.address, { rewriteAddress: true })
    }
    
    try {
        if (geocodeReverseAbort) geocodeReverseAbort.abort()
        geocodeReverseAbort = new AbortController()
        const res = await http.get('/geocode/reverse', { params: { lat, lon: lng }, signal: geocodeReverseAbort.signal })
        const payload = res.data?.result
        if (payload && payload.address) {
            const addr = payload.address;
            const province = _pickFirst(addr.province, addr.state, addr.region, addr.state_district)
            const city = _pickFirst(addr.city, addr.town, addr.municipality, addr.county, addr.state_district)
            const area = _pickFirst(addr.city_district, addr.district, addr.county, addr.suburb, addr.borough, addr.village)

            form.clockIn.location.province = province
            form.clockIn.location.city = city
            form.clockIn.location.area = area
            
            const fullAddr = _cleanSegment(payload.display_name)
            const place = _pickFirst(payload.name, fullAddr.split(',')[0])
            form.clockIn.location.address = _composeAddress([province, city, area, place])
            applyAddressStruct(form.clockIn.location.address, { rewriteAddress: true })
        }
    } catch (err) {
        if (err?.code === 'ERR_CANCELED') return
        console.error("逆地理编码失败", err);
        if (!label) {
             notifyWarning(err.response?.data?.detail || '无法自动获取详细地址，请手动填写')
        }
    }
}

const normalizeSearchQuery = (q) => {
  return String(q || '')
    .replace(/·/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
}

const goBack = () => {
  notifyInfo('正在返回上一页')
  router.back()
}

const searchPlace = async () => {
    const q = normalizeSearchQuery(searchQuery.value)
    if (!q) {
        notifyWarning('请输入要搜索的地点')
        return
    }
    if (!mapInstance.value) {
        return
    }
    try {
        if (geocodeSearchAbort) geocodeSearchAbort.abort()
        geocodeSearchAbort = new AbortController()
        const res = await http.get('/geocode/search', { params: { q }, signal: geocodeSearchAbort.signal })
        const results = res.data?.results || []
        if (!Array.isArray(results) || results.length === 0) {
            notifyWarning('没有搜索到结果，请换一个关键词')
            return
        }
        const best = results[0]
        const lat = Number(best.y)
        const lng = Number(best.x)

        if (best.bounds && Array.isArray(best.bounds)) {
            mapInstance.value.fitBounds(best.bounds, { padding: [20, 20] })
        } else {
            mapInstance.value.setView([lat, lng], 16)
        }
        updateLocation(lat, lng, best.label || q)
        notifySuccess('已定位到搜索结果')
    } catch (e) {
        if (e?.code === 'ERR_CANCELED') return
        notifyError(e.response?.data?.detail || '搜索失败，请稍后再试')
    }
}

const initMap = () => {
  if (mapInstance.value) return Promise.resolve()

  return ensureLeaflet().then((Leaflet) => {
    let lat = 30.5728
    let lng = 104.0668

    if (form.clockIn.location.latitude && form.clockIn.location.longitude) {
      const lat2 = parseFloat(form.clockIn.location.latitude)
      const lng2 = parseFloat(form.clockIn.location.longitude)
      if (Number.isFinite(lat2) && Number.isFinite(lng2)) {
        lat = lat2
        lng = lng2
      }
    }

    mapInstance.value = Leaflet.map('map').setView([lat, lng], 13)

    const tileProvider = String(import.meta.env.VITE_MAP_TILE_PROVIDER || '').trim().toLowerCase()
    const tdtKey = String(import.meta.env.VITE_TDT_KEY || '').trim()
    const useTdt = (tileProvider === 'tdt' || tileProvider === 'tianditu') || (!tileProvider && !!tdtKey)

    if (useTdt && tdtKey) {
      const subdomains = ['0', '1', '2', '3', '4', '5', '6', '7']
      Leaflet.tileLayer(`https://t{s}.tianditu.gov.cn/DataServer?T=vec_w&x={x}&y={y}&l={z}&tk=${encodeURIComponent(tdtKey)}`, {
        subdomains,
        maxZoom: 18,
        attribution: '© 天地图',
      }).addTo(mapInstance.value)
      Leaflet.tileLayer(`https://t{s}.tianditu.gov.cn/DataServer?T=cva_w&x={x}&y={y}&l={z}&tk=${encodeURIComponent(tdtKey)}`, {
        subdomains,
        maxZoom: 18,
        attribution: '© 天地图',
      }).addTo(mapInstance.value)
    } else {
      Leaflet.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '© OpenStreetMap contributors',
      }).addTo(mapInstance.value)
    }

    if (Number.isFinite(lat) && Number.isFinite(lng) && form.clockIn.location.latitude && form.clockIn.location.longitude) {
      marker.value = Leaflet.marker([lat, lng]).addTo(mapInstance.value)
    }

    mapInstance.value.on('click', async (e) => {
      const { lat, lng } = e.latlng
      updateLocation(lat, lng)
    })
  })
}

const fetchUser = async () => {
  if (!isEdit.value) return
  loading.value = true
  try {
    const res = await http.get(`/users/${route.params.id}`)
    Object.assign(form, res.data)
    const last = res.data?.last_execution_result || []
    if (Array.isArray(last)) {
      reportPreview.daily = (last.find(i => i?.task_type === '日报提交' && i?.report_content)?.report_content) || ''
      reportPreview.weekly = (last.find(i => i?.task_type === '周报提交' && i?.report_content)?.report_content) || ''
      reportPreview.monthly = (last.find(i => i?.task_type === '月报提交' && i?.report_content)?.report_content) || ''
    }
    if (!form.clockIn) {
      form.clockIn = {}
    }
    if (!form.clockIn.schedule) {
      form.clockIn.schedule = { startTime: '08:30', endTime: '18:30', weekdays: [1,2,3,4,5,6,7], totalDays: 180, startDate: '' }
    } else {
      if (!form.clockIn.schedule.startTime) form.clockIn.schedule.startTime = '08:30'
      if (!form.clockIn.schedule.endTime) form.clockIn.schedule.endTime = '18:30'
      if (!Array.isArray(form.clockIn.schedule.weekdays)) {
        form.clockIn.schedule.weekdays = Array.isArray(form.clockIn.customDays) && form.clockIn.customDays.length ? form.clockIn.customDays : [1,2,3,4,5,6,7]
      }
      if (!form.clockIn.schedule.totalDays) form.clockIn.schedule.totalDays = 180
      if (!form.clockIn.schedule.startDate) form.clockIn.schedule.startDate = ''
    }
    if (!form.reportSettings) {
      form.reportSettings = { daily: {}, weekly: {}, monthly: {} }
    }
    if (!form.reportSettings.daily) {
      form.reportSettings.daily = { enabled: false, imageCount: 0, submitTime: '12:00', submitDays: [1, 2, 3, 4, 5, 6, 7] }
    } else {
      if (!form.reportSettings.daily.submitTime) form.reportSettings.daily.submitTime = '12:00'
      if (!Array.isArray(form.reportSettings.daily.submitDays)) {
        form.reportSettings.daily.submitDays = [1, 2, 3, 4, 5, 6, 7]
      }
    }
    if (!form.reportSettings.weekly) {
      form.reportSettings.weekly = { enabled: true, imageCount: 0, submitTime: 5, submitAt: '18:30' }
    } else {
      if (typeof form.reportSettings.weekly.enabled !== 'boolean') form.reportSettings.weekly.enabled = true
      if (!form.reportSettings.weekly.submitTime) form.reportSettings.weekly.submitTime = 5
      if (!form.reportSettings.weekly.submitAt) form.reportSettings.weekly.submitAt = '18:30'
    }
    if (!form.reportSettings.monthly) {
      form.reportSettings.monthly = { enabled: false, imageCount: 0, submitTime: 29, submitAt: '12:00' }
    } else {
      if (!form.reportSettings.monthly.submitTime) form.reportSettings.monthly.submitTime = 29
      if (!form.reportSettings.monthly.submitAt) form.reportSettings.monthly.submitAt = '12:00'
    }
    form.pushNotifications = normalizePushNotifications(form.pushNotifications)
    await Promise.all([loadClockInMissingDays(), loadAllReportPeriodOptions()])
  } catch (error) {
    notifyError('加载失败')
  } finally {
    loading.value = false
  }
}

const _clone = (obj) => JSON.parse(JSON.stringify(obj))

const defaultFormSnapshot = _clone(form)

const resetToDefaultForm = () => {
  const next = _clone(defaultFormSnapshot)
  for (const k of Object.keys(form)) {
    if (!(k in next)) delete form[k]
  }
  for (const [k, v] of Object.entries(next)) {
    form[k] = v
  }
  reportPreview.daily = ''
  reportPreview.weekly = ''
  reportPreview.monthly = ''
  activeTab.value = 'basic'
  marker.value = null
  if (mapInstance.value) {
    mapInstance.value.off?.()
    mapInstance.value.remove?.()
    mapInstance.value = null
  }
}

const save = async () => {
  try {
    notifyInfo(isEdit.value ? '正在保存用户信息' : '正在创建用户')
    await flushUiMessage()
    form.clockIn.mode = 'custom'
    form.clockIn.customDays = form.clockIn.schedule.weekdays
    const payload = JSON.parse(JSON.stringify(form))
    payload.pushNotifications = normalizePushNotifications(payload.pushNotifications)
    if (isEdit.value) {
      await http.patch(`/users/${route.params.id}`, payload)
    } else {
      await http.post('/users', payload)
    }
    notifySuccess('保存成功')
    if (isEdit.value) {
      fetchUser()
    } else {
      resetToDefaultForm()
    }
  } catch (error) {
    notifyError(`保存失败：${resolveErrorMessage(error, '请求失败')}`)
  }
}

const cancelEdit = () => {
  if (isEdit.value) {
    fetchUser()
    notifyInfo('已撤销未保存的修改')
    return
  }
  resetToDefaultForm()
  notifyInfo('已清空未保存的内容')
}

const testAi = async () => {
  const apiUrl = (form.ai.apiUrl || '').trim()
  const apikey = (form.ai.apikey || '').trim()
  const model = (form.ai.model || '').trim()
  if (!apiUrl || !apikey || !model) {
    notifyWarning('请先填写 API URL、API Key 和 Model')
    return
  }
  aiTestLoading.value = true
  aiTestStatus.value = ''
  aiTestLatencyMs.value = null
  try {
    notifyInfo('正在测试 AI')
    await flushUiMessage()
    const res = await http.post('/ai/test', { apiUrl, apikey, model })
    aiTestStatus.value = res.data?.ok ? 'ok' : 'fail'
    aiTestLatencyMs.value = typeof res.data?.latency_ms === 'number' ? res.data.latency_ms : null
    notifySuccess('AI 可用')
  } catch (e) {
    aiTestStatus.value = 'fail'
    notifyError(resolveErrorMessage(e, 'AI 测试失败'))
  } finally {
    aiTestLoading.value = false
  }
}

const generateDailyReport = async () => {
  if (!isEdit.value) return
  aiDailyLoading.value = true
  try {
    notifyInfo('正在生成日报内容')
    await flushUiMessage()
    const res = await http.post(`/users/${route.params.id}/reports/daily/generate`)
    const content = res.data?.content
    if (typeof content === 'string') {
      reportPreview.daily = content
    }
    if (res.data?.already_submitted) {
      notifyWarning('检测到今天可能已提交过日报，仅生成内容供参考')
    } else {
      notifySuccess('已生成日报内容')
    }
  } catch (e) {
    notifyError(resolveErrorMessage(e, '生成失败'))
  } finally {
    aiDailyLoading.value = false
  }
}

const submitDailyReport = async () => {
  if (!isEdit.value) return
  const content = String(reportPreview.daily || '').trim()
  if (!content) {
    notifyWarning('请先生成或填写日报内容')
    return
  }
  submitDailyLoading.value = true
  try {
    notifyInfo('正在提交日报')
    await flushUiMessage()
    const res = await http.post(`/users/${route.params.id}/reports/daily/submit`, { content })
    notifySuccess(`提交成功：${res.data?.title || '日报'}`)
  } catch (e) {
    notifyError(resolveErrorMessage(e, '提交失败'))
  } finally {
    submitDailyLoading.value = false
  }
}

const reportLabelMap = { daily: '日报', weekly: '周报', monthly: '月报' }
const reportTaskMap = { daily: 'daily_report', weekly: 'weekly_report', monthly: 'monthly_report' }
const clockInMakeupTypeLabel = computed(() => (clockInMakeupType.value === 'END' ? '下班' : '上班'))
const reportPendingCount = (key) => (Array.isArray(reportPeriodOptions[key]) ? reportPeriodOptions[key].length : 0)
const reportMakeupAllInProgress = (key) => reportMakeupAllProgressKey.value === key && (reportMakeupAllLoading.value === key || reportMakeupAllPercent.value > 0)
const filteredClockInPeriodOptions = computed(() => {
  const options = Array.isArray(clockInPeriodOptions.value) ? clockInPeriodOptions.value : []
  return options
    .filter((item) => Array.isArray(item.missing_types) && item.missing_types.includes(clockInMakeupType.value))
    .map((item) => ({ ...item, label: `${item.value}（缺${clockInMakeupTypeLabel.value}）` }))
})
const syncClockInTargetFromOptions = () => {
  const options = filteredClockInPeriodOptions.value
  if (!options.length) {
    clockInTargetDates.value = []
    return
  }
  const validValues = new Set(options.map((item) => item.value))
  const selected = Array.isArray(clockInTargetDates.value) ? clockInTargetDates.value : []
  clockInTargetDates.value = selected.filter((item) => validValues.has(item))
  if (!clockInTargetDates.value.length) {
    clockInTargetDates.value = [options[0].value]
  }
}

const loadClockInMissingDays = async () => {
  if (!isEdit.value) return
  clockInPeriodLoading.value = true
  try {
    const res = await http.get(`/users/${route.params.id}/clock-in/missing-days`)
    clockInPeriodOptions.value = Array.isArray(res.data?.options) ? res.data.options : []
    clockInRecordCount.value = Number(res.data?.record_count || 0)
    syncClockInTargetFromOptions()
  } catch (e) {
    clockInPeriodOptions.value = []
    clockInRecordCount.value = 0
    clockInTargetDates.value = []
    notifyWarning(resolveErrorMessage(e, '获取缺卡日期失败'))
  } finally {
    clockInPeriodLoading.value = false
  }
}

const makeupClockIn = async () => {
  if (!isEdit.value) return
  const targetDates = Array.isArray(clockInTargetDates.value) ? clockInTargetDates.value : []
  if (!targetDates.length) {
    notifyWarning('暂无可补卡日期')
    return
  }
  clockInMakeupLoading.value = true
  try {
    notifyInfo('正在补卡')
    await flushUiMessage()
    const res = await http.post(`/users/${route.params.id}/clock-in/makeup`, {
      target_dates: targetDates,
      target_type: clockInMakeupType.value,
    }, { timeout: CLOCKIN_MAKEUP_REQUEST_TIMEOUT })
    const result = res.data?.result || {}
    if (result.status === 'success') {
      notifySuccess(result.message || '补卡完成')
    } else if (result.status === 'skip') {
      notifyWarning(result.message || '已跳过补卡')
    } else {
      notifyError(result.message || '补卡失败')
    }
    await loadClockInMissingDays()
  } catch (e) {
    notifyError(resolveErrorMessage(e, '补卡失败'))
  } finally {
    clockInMakeupLoading.value = false
  }
}

const makeupAllClockIn = async () => {
  if (!isEdit.value) return
  if (!filteredClockInPeriodOptions.value.length) {
    notifyWarning('暂无待补卡日期')
    return
  }
  clockInMakeupAllLoading.value = true
  startClockInMakeupAllProgress(filteredClockInPeriodOptions.value.length)
  try {
    notifyInfo('正在补全部待补日期')
    await flushUiMessage()
    const res = await http.post(`/users/${route.params.id}/clock-in/makeup-all`, {
      target_type: clockInMakeupType.value,
    }, { timeout: CLOCKIN_MAKEUP_REQUEST_TIMEOUT })
    const result = res.data?.result || {}
    if (result.status === 'success') {
      notifySuccess(result.message || '全部补卡完成')
    } else if (result.status === 'skip') {
      notifyWarning(result.message || '已跳过补卡')
    } else {
      notifyError(result.message || '全部补卡失败')
    }
    finishClockInMakeupAllProgress()
    await loadClockInMissingDays()
  } catch (e) {
    resetClockInMakeupAllProgress()
    notifyError(resolveErrorMessage(e, '全部补卡失败'))
  } finally {
    clockInMakeupAllLoading.value = false
  }
}

const getReportTarget = (key) => String(reportTargets[key] || '').trim() || undefined
const hasReportTarget = (key) => !!getReportTarget(key)
const syncReportTargetFromOptions = (key) => {
  const options = Array.isArray(reportPeriodOptions[key]) ? reportPeriodOptions[key] : []
  if (!options.length) {
    reportTargets[key] = ''
    return
  }
  if (!options.some((item) => item.value === reportTargets[key])) {
    reportTargets[key] = options[0].value
  }
}

const loadReportPeriodOptions = async (key) => {
  if (!isEdit.value) return
  reportPeriodLoading[key] = true
  try {
    const res = await http.get(`/users/${route.params.id}/reports/${key}/missing-periods`)
    reportPeriodOptions[key] = Array.isArray(res.data?.options) ? res.data.options : []
    syncReportTargetFromOptions(key)
  } catch (e) {
    reportPeriodOptions[key] = []
    reportTargets[key] = ''
    notifyWarning(resolveErrorMessage(e, `获取${reportLabelMap[key] || '报告'}未提交周期失败`))
  } finally {
    reportPeriodLoading[key] = false
  }
}

const loadAllReportPeriodOptions = async () => {
  if (!isEdit.value) return
  await Promise.all(['daily', 'weekly', 'monthly'].map((key) => loadReportPeriodOptions(key)))
}

const generateReport = async (key) => {
  if (!isEdit.value) return
  const label = reportLabelMap[key] || '报告'
  if (!hasReportTarget(key)) {
    notifyWarning(`暂无可补交的${label}周期`)
    return
  }
  reportActionLoading.value = `${key}_generate`
  try {
    notifyInfo(`正在生成${label}`)
    await flushUiMessage()
    const res = await http.post(`/users/${route.params.id}/reports/${key}/generate`, null, {
      params: { target_period: getReportTarget(key) },
    })
    reportPreview[key] = String(res.data?.content || '')
    notifySuccess(`${label}已生成`)
  } catch (e) {
    notifyError(resolveErrorMessage(e, `${label}生成失败`))
  } finally {
    reportActionLoading.value = ''
  }
}

const submitReport = async (key) => {
  if (!isEdit.value) return
  const label = reportLabelMap[key] || '报告'
  if (!hasReportTarget(key)) {
    notifyWarning(`暂无可补交的${label}周期`)
    return
  }
  const content = String(reportPreview[key] || '').trim()
  if (!content) {
    notifyWarning(`请先生成或填写${label}内容`)
    return
  }
  reportActionLoading.value = `${key}_submit`
  try {
    notifyInfo(`正在提交${label}`)
    await flushUiMessage()
    const res = await http.post(`/users/${route.params.id}/reports/${key}/submit`, {
      content,
      target_period: getReportTarget(key),
    })
    notifySuccess(`提交成功：${res.data?.title || label}`)
    await loadReportPeriodOptions(key)
  } catch (e) {
    notifyError(resolveErrorMessage(e, `${label}提交失败`))
  } finally {
    reportActionLoading.value = ''
  }
}

const makeupAllReports = async (key) => {
  if (!isEdit.value) return
  const label = reportLabelMap[key] || '报告'
  const total = reportPendingCount(key)
  if (!total) {
    notifyWarning(`暂无待补交${label}周期`)
    return
  }
  reportMakeupAllLoading.value = key
  startReportMakeupAllProgress(key, total)
  try {
    notifyInfo(`正在补交全部待补${label}`)
    await flushUiMessage()
    const res = await http.post(`/users/${route.params.id}/reports/${key}/makeup-all`, null, {
      timeout: REPORT_MAKEUP_REQUEST_TIMEOUT,
    })
    const result = res.data?.result || {}
    if (result.status === 'success') {
      notifySuccess(result.message || `全部${label}补交完成`)
    } else if (result.status === 'skip') {
      notifyWarning(result.message || `已跳过${label}补交`)
    } else {
      notifyError(result.message || `全部${label}补交失败`)
    }
    finishReportMakeupAllProgress()
    await fetchUser()
    await loadReportPeriodOptions(key)
  } catch (e) {
    resetReportMakeupAllProgress()
    notifyError(resolveErrorMessage(e, `全部${label}补交失败`))
  } finally {
    reportMakeupAllLoading.value = ''
  }
}

const runReportNow = async (taskType) => {
  if (!isEdit.value) return
  const labelMap = {
    daily_report: '日报',
    weekly_report: '周报',
    monthly_report: '月报',
  }
  const label = labelMap[taskType] || '报告'
  const key = Object.keys(reportTaskMap).find((item) => reportTaskMap[item] === taskType)
  if (!hasReportTarget(key)) {
    notifyWarning(`暂无可补交的${label}周期`)
    return
  }
  reportRunLoading.value = taskType
  try {
    notifyInfo(`正在立即执行${label}`)
    await flushUiMessage()
    const res = await http.post(`/users/${route.params.id}/run`, {
      task_type: taskType,
      force_report: true,
      target_period: getReportTarget(key),
    })
    const results = Array.isArray(res.data?.results) ? res.data.results : []
    const first = results[0] || {}
    if (first.report_content) {
      if (taskType === 'daily_report') reportPreview.daily = first.report_content
      if (taskType === 'weekly_report') reportPreview.weekly = first.report_content
      if (taskType === 'monthly_report') reportPreview.monthly = first.report_content
    }
    if (first.status === 'success') {
      notifySuccess(first.message || `${label}已提交`)
    } else if (first.status === 'skip') {
      notifyWarning(first.message || `${label}已跳过`)
    } else {
      notifyError(first.message || `${label}执行失败`)
    }
    await fetchUser()
    if (key) await loadReportPeriodOptions(key)
  } catch (e) {
    notifyError(resolveErrorMessage(e, `${label}执行失败`))
  } finally {
    reportRunLoading.value = ''
  }
}

const applyModelScopePreset = () => {
  form.ai.apiUrl = 'https://api-inference.modelscope.cn/v1'
  form.ai.model = 'Qwen/Qwen3-Next-80B-A3B-Instruct'
  aiTestStatus.value = ''
  aiTestLatencyMs.value = null
  notifySuccess('已填入魔搭预设，请粘贴 Token 后点击“测试 AI”')
}

const _pickBestAddress = (...values) => {
  const list = []
  for (const v of values.flat(2)) {
    const s = String(v || '').trim()
    if (s) list.push(s)
  }
  if (!list.length) return ''
  const unique = []
  for (const s of list) {
    if (!unique.includes(s)) unique.push(s)
  }
  unique.sort((x, y) => y.length - x.length)
  return unique[0] || ''
}

const fillFromAccountAddress = async () => {
  if (!isEdit.value) {
    notifyWarning('请先保存用户后再自动填充')
    return
  }
  addrFillLoading.value = true
  try {
    notifyInfo('正在读取账号地址并自动填充')
    await flushUiMessage()
    const res = await http.get(`/users/${route.params.id}/account-address`)
    const bestAddr = _pickBestAddress(res.data?.address, res.data?.addressCandidates, res.data?.maskedAddress, res.data?.maskedCandidates)
    if (!bestAddr) {
      notifyWarning('未获取到账号详细地址')
      return
    }
    form.clockIn.location.address = bestAddr
    searchQuery.value = bestAddr
    if (!mapInstance.value) {
      await initMap()
      await nextTick()
    }
    await searchPlace()
    notifySuccess('已填入账号详细地址，并尝试自动定位经纬度')
  } catch (e) {
    notifyError(resolveErrorMessage(e, '自动填充失败'))
  } finally {
    addrFillLoading.value = false
  }
}

watch(activeTab, (val) => {
    if (val === 'clockin') {
        nextTick(() => {
            initMap().then(() => {
              setTimeout(() => {
                  mapInstance.value?.invalidateSize();
              }, 100);
            })
        })
    }
});

onMounted(async () => {
    await fetchUser();
    if (activeTab.value === 'clockin') {
        await initMap()
    }
})

let addrStructTimer = null
watch(
  () => form.clockIn?.location?.address,
  (val) => {
    const s = String(val || '').trim()
    if (!s) return
    if (!/[·•]/.test(s)) return
    if (addrStructTimer) clearTimeout(addrStructTimer)
    addrStructTimer = setTimeout(() => {
      applyAddressStruct(s)
    }, 350)
  }
)

const updateIsMobile = () => {
  isMobile.value = window.matchMedia('(max-width: 768px)').matches
}

onMounted(() => {
  updateIsMobile()
  let raf = 0
  const onResize = () => {
    if (raf) return
    raf = requestAnimationFrame(() => {
      raf = 0
      updateIsMobile()
    })
  }
  window.addEventListener('resize', onResize)
  onUnmounted(() => {
    if (raf) cancelAnimationFrame(raf)
    window.removeEventListener('resize', onResize)
  })
})
onUnmounted(() => {
  if (addrStructTimer) clearTimeout(addrStructTimer)
  resetClockInMakeupAllProgress()
  resetReportMakeupAllProgress()
  if (geocodeSearchAbort) geocodeSearchAbort.abort()
  if (geocodeReverseAbort) geocodeReverseAbort.abort()
  if (mapInstance.value) {
    mapInstance.value.off?.()
    mapInstance.value.remove?.()
    mapInstance.value = null
  }
  marker.value = null
})
</script>

<style scoped>
.desktop-actions {
  margin-top: 20px;
}
.map {
  height: 300px;
  margin-bottom: 20px;
  border-radius: 4px;
  border: 1px solid var(--el-border-color);
  z-index: 1;
}
.place-row {
  display: flex;
  gap: 10px;
  width: 100%;
}
.place-input {
  flex: 1;
  min-width: 0;
}
.place-actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
  flex-wrap: wrap;
  justify-content: flex-end;
}
.place-actions :deep(.el-button) {
  white-space: nowrap;
}
.week-group {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}
.clockin-backfill {
  width: 100%;
}
.clockin-backfill-row {
  width: 100%;
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}
.clockin-backfill-row :deep(.el-select) {
  flex: 1 1 240px;
}
.clockin-backfill-row :deep(.clockin-type-select) {
  flex: 0 0 120px;
}
.clockin-backfill-progress {
  margin-top: 10px;
  max-width: 520px;
}
.clockin-backfill-hint {
  margin-top: 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.report-backfill {
  width: 100%;
}
.report-backfill-row {
  width: 100%;
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}
.report-backfill-row :deep(.el-select) {
  flex: 1 1 240px;
}
.report-backfill-progress {
  margin-top: 10px;
  max-width: 520px;
}
.report-backfill-hint {
  margin-top: 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.form-actions {
  display: flex;
  gap: 12px;
}
.mobile-actions-spacer {
  height: calc(68px + env(safe-area-inset-bottom));
}
.mobile-bottom-actions {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  border-top: 1px solid var(--el-border-color);
  background-color: var(--el-bg-color);
  padding: 10px calc(12px + env(safe-area-inset-left)) calc(10px + env(safe-area-inset-bottom)) calc(12px + env(safe-area-inset-right));
  z-index: 1500;
}
.mobile-bottom-actions-inner {
  max-width: 1200px;
  margin: 0 auto;
  display: flex;
  gap: 12px;
}
.ai-test-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.ai-test-meta {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.report-preview {
  width: 100%;
}
.report-preview-meta {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-bottom: 6px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.report-preview-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  flex-wrap: wrap;
}
@media (max-width: 768px) {
  .map {
    height: 240px;
    margin-bottom: 16px;
  }
  .place-actions {
    margin-top: 8px;
  }
  .place-row {
    flex-direction: column;
  }
  .place-actions {
    width: 100%;
    justify-content: stretch;
  }
  .place-actions :deep(.el-button) {
    flex: 1;
  }
}
</style>
