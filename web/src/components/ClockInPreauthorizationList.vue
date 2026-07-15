<script setup>
import { computed } from 'vue'

const props = defineProps({
  items: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  page: { type: Number, default: 1 },
  pageSize: { type: Number, default: 20 },
  total: { type: Number, default: 0 },
  emptyText: { type: String, default: '暂无预授权日期' },
})

const emit = defineEmits(['authorize', 'page-change'])

const statusMeta = {
  pending: { label: '待授权', type: 'info' },
  authorized: { label: '已授权', type: 'success' },
  consumed: { label: '已使用', type: '' },
  reauthorize_required: { label: '需重新授权', type: 'warning' },
}

const hasPagination = computed(() => props.total > props.pageSize)
const rowKey = (item) => `${item.target_date}-${item.target_type}`

const targetTypeLabel = (item) => {
  const type = String(item?.target_type || '').toUpperCase()
  if (type === 'START') return '上班'
  if (type === 'END') return '下班'
  return '补卡'
}

const targetTimeLabel = (item) => {
  if (item?.target_type === 'MAKEUP') return '上班或下班'
  return item?.target_time || '-'
}

const usedTypeLabel = (item) => {
  if (item?.status !== 'consumed') return ''
  if (item?.used_target_type === 'START') return '已用于上班打卡'
  if (item?.used_target_type === 'END') return '已用于下班打卡'
  return '已用于打卡'
}

const statusLabel = (status) => statusMeta[status]?.label || '未知状态'
const statusType = (status) => statusMeta[status]?.type || 'info'

const actionLabel = (item) => {
  if (item?.status === 'pending') return '开始预授权'
  return '重新预授权'
}

const formatDateTime = (value) => {
  if (!value) return '-'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return String(value)
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(parsed)
}
</script>

<template>
  <div class="preauthorization-list" v-loading="loading">
    <el-table
      v-if="items.length || loading"
      class="desktop-table"
      :data="items"
      :row-key="rowKey"
      size="small"
    >
      <el-table-column prop="target_date" label="日期" min-width="118" />
      <el-table-column label="类型" min-width="92">
        <template #default="scope">
          {{ targetTypeLabel(scope.row) }}
        </template>
      </el-table-column>
      <el-table-column label="计划时间" min-width="124">
        <template #default="scope">
          {{ targetTimeLabel(scope.row) }}
        </template>
      </el-table-column>
      <el-table-column label="状态" min-width="142">
        <template #default="scope">
          <div class="status-cell">
            <el-tag size="small" :type="statusType(scope.row.status)">
              {{ statusLabel(scope.row.status) }}
            </el-tag>
            <span v-if="usedTypeLabel(scope.row)" class="used-type">
              {{ usedTypeLabel(scope.row) }}
            </span>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="授权时间" min-width="178">
        <template #default="scope">
          {{ formatDateTime(scope.row.authorized_at) }}
        </template>
      </el-table-column>
      <el-table-column label="操作" width="126" align="right" fixed="right">
        <template #default="scope">
          <el-button
            v-if="scope.row.can_authorize"
            size="small"
            type="primary"
            plain
            @click="emit('authorize', scope.row)"
          >
            {{ actionLabel(scope.row) }}
          </el-button>
          <span v-else class="action-complete">无需操作</span>
        </template>
      </el-table-column>
    </el-table>

    <div v-if="items.length" class="mobile-list">
      <div
        v-for="item in items"
        :key="`${item.target_date}-${item.target_type}`"
        class="mobile-row"
      >
        <div class="mobile-row-main">
          <div class="mobile-date">{{ item.target_date }}</div>
          <div class="mobile-target">
            {{ targetTypeLabel(item) }} · {{ targetTimeLabel(item) }}
          </div>
        </div>
        <div class="mobile-status">
          <el-tag size="small" :type="statusType(item.status)">
            {{ statusLabel(item.status) }}
          </el-tag>
          <span v-if="usedTypeLabel(item)" class="used-type">{{ usedTypeLabel(item) }}</span>
          <span v-else-if="item.authorized_at" class="authorized-time">
            {{ formatDateTime(item.authorized_at) }}
          </span>
        </div>
        <el-button
          v-if="item.can_authorize"
          class="mobile-action"
          size="small"
          type="primary"
          plain
          @click="emit('authorize', item)"
        >
          {{ actionLabel(item) }}
        </el-button>
      </div>
    </div>

    <el-empty v-if="!loading && !items.length" :description="emptyText" />

    <div v-if="hasPagination" class="pagination-row">
      <el-pagination
        background
        layout="prev, pager, next"
        :current-page="page"
        :page-size="pageSize"
        :total="total"
        @current-change="emit('page-change', $event)"
      />
    </div>
  </div>
</template>

<style scoped>
.preauthorization-list {
  min-height: 160px;
}

.status-cell {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.used-type,
.authorized-time,
.action-complete {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.mobile-list {
  display: none;
}

.pagination-row {
  display: flex;
  justify-content: flex-end;
  padding-top: 16px;
}

@media (max-width: 768px) {
  .desktop-table {
    display: none;
  }

  .mobile-list {
    display: grid;
  }

  .mobile-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 10px 12px;
    align-items: center;
    padding: 14px 0;
    border-bottom: 1px solid var(--el-border-color-lighter);
  }

  .mobile-row:first-child {
    padding-top: 0;
  }

  .mobile-row-main,
  .mobile-status {
    min-width: 0;
  }

  .mobile-date {
    font-weight: 700;
    color: var(--el-text-color-primary);
  }

  .mobile-target {
    margin-top: 4px;
    font-size: 12px;
    color: var(--el-text-color-secondary);
  }

  .mobile-status {
    display: flex;
    align-items: flex-end;
    gap: 5px;
    flex-direction: column;
  }

  .mobile-action {
    grid-column: 1 / -1;
    width: 100%;
  }

  .pagination-row {
    justify-content: center;
  }
}
</style>
