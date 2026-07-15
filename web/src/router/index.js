import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { useUserAuthStore } from '../stores/userAuth'

const routes = [
  { path: '/login', component: () => import('../views/Login.vue'), meta: { public: true, area: 'admin' } },
  { path: '/u/login', component: () => import('../views/user/UserLogin.vue'), meta: { public: true, area: 'user' } },
  { path: '/u/register', component: () => import('../views/user/UserRegister.vue'), meta: { public: true, area: 'user' } },
  {
    path: '/u',
    component: () => import('../views/user/UserLayout.vue'),
    meta: { area: 'user' },
    children: [
      { path: '', component: () => import('../views/user/UserHome.vue'), meta: { area: 'user' } },
    ],
  },
  {
    path: '/u/settings',
    component: () => import('../views/user/UserLayout.vue'),
    meta: { area: 'user' },
    children: [
      { path: '', component: () => import('../views/user/UserSettings.vue'), meta: { area: 'user' } },
    ],
  },
  {
    path: '/u/preauthorizations',
    component: () => import('../views/user/UserLayout.vue'),
    meta: { area: 'user' },
    children: [
      { path: '', component: () => import('../views/user/UserPreauthorizations.vue'), meta: { area: 'user' } },
    ],
  },
  { path: '/', component: () => import('../views/UserList.vue'), meta: { permissions: ['users:read'] } },
  { path: '/audit', component: () => import('../views/AuditLogs.vue'), meta: { permissions: ['audit:read'] } },
  { path: '/settings', component: () => import('../views/NotificationSettings.vue'), meta: { permissions: ['settings:manage'] } },
  { path: '/settings/notifications', component: () => import('../views/NotificationSettings.vue'), meta: { permissions: ['settings:manage'] } },
  { path: '/create', component: () => import('../views/UserEdit.vue'), meta: { permissions: ['users:write'] } },
  { path: '/edit/:id', component: () => import('../views/UserEdit.vue'), meta: { permissions: ['users:write'] } },
  { path: '/users/:id/preauthorizations', component: () => import('../views/UserPreauthorizations.vue'), meta: { permissions: ['tasks:run'] } },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

const isUserRoute = (path) => path === '/u' || path.startsWith('/u/')
const isUserPublicRoute = (path) => path === '/u/login' || path === '/u/register'
const isRootRoute = (path) => path === '/'
const validateAdminSessionInBackground = (auth) => {
  if (auth.sessionChecked || !auth.isAuthed) return
  void auth.validateSession()
}

const validateUserSessionInBackground = (userAuth) => {
  if (userAuth.sessionChecked || !userAuth.isAuthed) return
  void userAuth.validateSession()
}

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  const userAuth = useUserAuthStore()

  if (isUserRoute(to.path)) {
    if (isUserPublicRoute(to.path)) {
      if (userAuth.sessionChecked && userAuth.isAuthed) return '/u'
      validateUserSessionInBackground(userAuth)
      return true
    }
    if (!userAuth.sessionChecked && !(await userAuth.validateSession())) {
      return { path: '/u/login', query: { redirect: to.fullPath } }
    }
    if (!userAuth.isAuthed) {
      return { path: '/u/login', query: { redirect: to.fullPath } }
    }
    return true
  }

  if (isRootRoute(to.path)) {
    if ((!auth.sessionChecked && await auth.validateSession()) || auth.isAuthed) return true
    if ((!userAuth.sessionChecked && await userAuth.validateSession()) || userAuth.isAuthed) return '/u'
    return { path: '/u/login', query: { redirect: '/u' } }
  }

  if (to.meta.public) {
    if (auth.sessionChecked && auth.isAuthed && to.path === '/login') return '/'
    validateAdminSessionInBackground(auth)
    return true
  }
  if (!auth.sessionChecked && !(await auth.validateSession())) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }
  if (!auth.isAuthed) return { path: '/login', query: { redirect: to.fullPath } }
  const permissions = Array.isArray(to.meta.permissions) ? to.meta.permissions : []
  if (permissions.length > 0) {
    if (permissions.every((permission) => auth.can(permission))) return true
    if (to.path !== '/') return '/'
    auth.logout()
    return { path: '/login' }
  }
  const roles = to.meta.roles
  if (!roles) return true
  if (roles.includes(auth.role)) return true
  if (to.path !== '/') return '/'
  auth.logout()
  return { path: '/login' }
})

export default router
