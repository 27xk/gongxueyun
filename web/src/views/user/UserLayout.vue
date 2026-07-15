<template>
  <div class="user-layout">
    <div class="user-header">
      <div class="user-header-main">
        <div>
          <div class="user-title">工学云用户端</div>
          <div class="user-subtitle">{{ auth.phone || '已登录用户' }}</div>
        </div>
        <div class="user-actions">
          <el-button plain @click="router.push('/u')">首页</el-button>
          <el-button plain @click="router.push('/u/preauthorizations')">预授权</el-button>
          <el-button plain @click="router.push('/u/settings')">设置</el-button>
          <el-button type="danger" plain @click="logout">退出</el-button>
        </div>
      </div>
    </div>
    <div class="user-body">
      <router-view />
    </div>
  </div>
</template>

<script setup>
import { useRouter } from 'vue-router'
import { userHttp } from '../../api/userHttp'
import { useUserAuthStore } from '../../stores/userAuth'

const router = useRouter()
const auth = useUserAuthStore()

const logout = async () => {
  try {
    await userHttp.post('/app/auth/logout')
  } catch {
    // Cookie 可能已经过期，本地状态仍然需要清理。
  }
  auth.logout()
  router.replace('/u/login')
}
</script>

<style scoped>
.user-layout {
  min-height: 100vh;
  min-height: 100dvh;
  background: var(--el-bg-color-page);
}
.user-header {
  position: sticky;
  top: 0;
  z-index: 10;
  border-bottom: 1px solid var(--el-border-color-light);
  background: color-mix(in srgb, var(--el-bg-color) 92%, transparent);
  backdrop-filter: blur(10px);
}
.user-header-main {
  max-width: 1200px;
  margin: 0 auto;
  padding: 14px 16px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.user-title {
  font-size: 18px;
  font-weight: 800;
}
.user-subtitle {
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.user-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.user-body {
  max-width: 1200px;
  margin: 0 auto;
  padding: 16px;
}
@media (max-width: 768px) {
  .user-header-main {
    padding: 12px;
  }
  .user-body {
    padding: 12px;
  }
  .user-actions {
    width: 100%;
  }
  .user-actions :deep(.el-button) {
    flex: 1 1 0;
  }
}
</style>
